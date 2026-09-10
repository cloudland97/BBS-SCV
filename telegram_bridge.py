"""Telegram bridge — thin hub that wires handlers and starts polling.

Split modules:
  tg_config.py      — env, paths, constants, mutable globals
  tg_utils.py       — message splitting, HUD, formatting helpers
  discord_helpers.py — Discord API helpers, username cache, DM, bongnews
"""

import asyncio
import atexit
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from zoneinfo import ZoneInfo

import httpx
from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from tg_config import (
    ALLOWED_USER_ID,
    BBS_DB_PATH,
    BOT_TOKEN,
    LOG_DIR,
    LOOP_ALERT_COOLDOWN_SEC,
    LOOP_ALERT_THRESHOLDS,
    PROJECTS,
    check_shared_schema,
    get_bbs_db,
    log,
)
from tg_utils import ensure_utc, md_escape, safe_reply, safe_send
from discord_helpers import _broadcast_bbnews, _broadcast_bbnews_auto
import cmd_news_schedule as _cns


# -- Loop monitor --

_loop_alert_last: dict[str, float] = {}


async def _loop_monitor_task(app):
    """Background task: alert if a loop hasn't run within its threshold."""
    await asyncio.sleep(60)
    while True:
        await asyncio.sleep(300)
        if not BBS_DB_PATH.exists():
            continue
        try:
            now_utc = datetime.now(timezone.utc)
            conn = get_bbs_db()
            try:
                rows = conn.execute("SELECT loop_id, last_run FROM loop_health").fetchall()
            finally:
                conn.close()

            alerts = []
            for row in rows:
                loop_id = row["loop_id"]
                threshold_mins = LOOP_ALERT_THRESHOLDS.get(loop_id, 180)
                last_dt = ensure_utc(datetime.fromisoformat(row["last_run"]))
                diff_mins = (now_utc - last_dt).total_seconds() / 60
                if diff_mins > threshold_mins:
                    last_alerted = _loop_alert_last.get(loop_id, 0)
                    if time.time() - last_alerted > LOOP_ALERT_COOLDOWN_SEC:
                        _loop_alert_last[loop_id] = time.time()
                        h, m = int(diff_mins // 60), int(diff_mins % 60)
                        age = f"{h}h{m}m" if h else f"{m}m"
                        alerts.append(f"⚠️ `{loop_id}` — {age} no response")

            if alerts:
                msg = "Loop down detected\n\n" + "\n".join(alerts)
                await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=msg, parse_mode="Markdown")
                log.warning(f"Loop alert: {alerts}")
        except Exception as e:
            log.error(f"Loop monitor error: {e}")


# -- News scheduler --


def _send_status(news_id, ok_count: int, fail_count: int) -> str:
    """(ok, fail) -> 'sent' / 'failed' / 'pending' (the (0,0) case, up to MAX_UNATTEMPTED_ATTEMPTS)."""
    if ok_count:
        _cns.forget_attempts(news_id)
        return "sent"
    if fail_count:
        _cns.forget_attempts(news_id)
        return "failed"
    if _cns.record_unattempted(news_id) >= _cns.MAX_UNATTEMPTED_ATTEMPTS:
        _cns.forget_attempts(news_id)
        return "failed"
    return "pending"


async def _report_send_outcome(bot, label: str, news_id, ok_count, fail_count, status) -> None:
    """Log a broadcast result, and tell the operator once when retrying stops.

    'failed' with fail_count 0 means the retry budget was exhausted (a real
    delivery failure always attempts >=1 recipient) — the one case worth a message.
    """
    if status == "sent":
        return
    if status == "failed" and fail_count:
        log.error(
            "%s news id=%s reached no subscriber (%d attempted, all failed)",
            label,
            news_id,
            fail_count,
        )
        return
    if status == "failed":
        log.error(
            "%s news id=%s not attempted %d times; giving up",
            label,
            news_id,
            _cns.MAX_UNATTEMPTED_ATTEMPTS,
        )
        await safe_send(
            bot,
            ALLOWED_USER_ID,
            f"❌ 예약 뉴스 `[{news_id}]` 발송 포기: "
            f"{_cns.MAX_UNATTEMPTED_ATTEMPTS}회 시도했지만 보낼 대상이 없었습니다 "
            "(Discord 토큰 없음 또는 봉봉뉴스 구독자 없음). `failed` 처리되어 "
            "재시도하지 않습니다. 원인 해결 후 `!news`로 다시 예약하세요.",
        )
        return
    log.warning(
        "%s news id=%s was not attempted (no token or no subscribers); "
        "left pending for the next cycle",
        label,
        news_id,
    )


async def _check_boot_scheduled_news(app):
    """Run the boot news check without ever aborting startup — post_init runs
    before polling begins, so anything raised in here would kill the bot outright."""
    try:
        await _boot_scheduled_news(app)
    except Exception as e:
        log.error(f"Boot scheduled-news check failed: {e}", exc_info=True)


async def _boot_scheduled_news(app):
    """On boot: send news due within 15 min; expire and notify anything older.

    Also detect rows orphaned in 'sending' by a crash mid-send and warn the admin.
    This is detect-and-notify only — never auto-resend, because a crash AFTER a
    successful broadcast (but before mark 'sent') would otherwise double-send.
    """
    now_utc = datetime.now(timezone.utc)
    KST = ZoneInfo("Asia/Seoul")

    # Stale-'sending' recovery: rows claimed >5 min ago that never reached 'sent'.
    # A human decides whether they actually went out; we only surface them.
    stale = await _cns.a_get_stale_sending(now_utc - timedelta(minutes=5))
    if stale:
        lines = [
            f"⚠️ 발송 중(sending) 상태로 멈춘 예약 뉴스 {len(stale)}건 — 발송 여부 수동 확인 필요:"
        ]
        for item in stale:
            claimed_kst = (
                datetime.fromisoformat(item["claimed_at"])
                .replace(tzinfo=timezone.utc)
                .astimezone(KST)
            )
            lines.append(
                f"• `[{item['id']}]` {claimed_kst.strftime('%m/%d %H:%M')} claimed — {md_escape(item['content'][:30])}..."
            )
        await safe_send(app.bot, ALLOWED_USER_ID, "\n".join(lines))

    cutoff = now_utc - timedelta(minutes=15)
    due = await _cns.a_get_due(now_utc)
    if not due:
        return

    send_now, expired = [], []
    for item in due:
        fired_at = datetime.fromisoformat(item["scheduled_at"]).replace(tzinfo=timezone.utc)
        (send_now if fired_at >= cutoff else expired).append(item)

    if expired:
        lines = [f"⚠️ 봇 다운 중 만료된 예약 뉴스 {len(expired)}건:"]
        for item in expired:
            dt_kst = (
                datetime.fromisoformat(item["scheduled_at"])
                .replace(tzinfo=timezone.utc)
                .astimezone(KST)
            )
            lines.append(
                f"• `[{item['id']}]` {dt_kst.strftime('%m/%d %H:%M')} — {md_escape(item['content'][:30])}..."
            )
            await _cns.a_set_status_if_pending(item["id"], "expired")
        await safe_send(app.bot, ALLOWED_USER_ID, "\n".join(lines))

    for item in send_now:
        # Same claim-before-send guard as the 30s loop (the scheduler task also
        # starts on boot, so both could race the same due row).
        if not await _cns.a_claim_for_send(item["id"]):
            continue
        sent_ok = False
        try:
            ok_count, fail_count = await _broadcast_bbnews_auto(
                app.bot, ALLOWED_USER_ID, item["content"]
            )
            # The DMs are out. Nothing after this point may re-queue the row.
            sent_ok = True
            status = _send_status(item["id"], ok_count, fail_count)
            await _cns.a_mark_status(item["id"], status)
            await _report_send_outcome(app.bot, "Boot", item["id"], ok_count, fail_count, status)
        except Exception as e:
            if sent_ok:
                # Only the status write failed. Leave the row 'sending' —
                # terminal for the scheduler (get_due is pending-only), so !newslist surfaces it for a human.
                log.error(f"Boot scheduled news status write failed id={item['id']}: {e}")
            else:
                # Send did not complete -> back to 'pending' for retry.
                await _cns.a_mark_status(item["id"], "pending")
                log.error(f"Boot scheduled news send failed id={item['id']}: {e}")


async def _news_scheduler_task(app):
    """Background task: fire scheduled news when due (checks every 30s)."""
    await asyncio.sleep(30)
    while True:
        await asyncio.sleep(30)
        if not BBS_DB_PATH.exists():
            continue  # standalone clone / BBS bot not set up yet
        try:
            due = await _cns.a_get_due(datetime.now(timezone.utc))
            for item in due:
                # Claim BEFORE broadcasting: only one winner flips 'pending' ->
                # 'sending', so get_due won't re-pick this row next cycle -> no
                # duplicate. A lost claim means another cycle already has it.
                if not await _cns.a_claim_for_send(item["id"]):
                    continue
                log.info(f"Sending scheduled news id={item['id']}")
                sent_ok = False
                try:
                    ok_count, fail_count = await _broadcast_bbnews_auto(
                        app.bot, ALLOWED_USER_ID, item["content"]
                    )
                    # The DMs are out. Nothing after this point may re-queue it.
                    sent_ok = True
                    status = _send_status(item["id"], ok_count, fail_count)
                    await _cns.a_mark_status(item["id"], status)
                    await _report_send_outcome(
                        app.bot, "Scheduled", item["id"], ok_count, fail_count, status
                    )
                except Exception as e:
                    if sent_ok:
                        # Only the status write failed; the row stays 'sending',
                        # which get_due never re-picks, so it is not re-sent.
                        log.error(f"Scheduled news status write failed id={item['id']}: {e}")
                    else:
                        # Send did NOT complete -> revert to 'pending' so it retries.
                        await _cns.a_mark_status(item["id"], "pending")
                        log.error(f"Scheduled news send failed id={item['id']}: {e}")
        except Exception as e:
            log.error(f"News scheduler error: {e}")


# -- Photo / video / document handlers --


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    if caption.startswith("!news2"):
        await _handle_news_media(update, context, caption, update.message.photo[-1])
        return
    await update.message.reply_text("Use `!news2 content` caption to broadcast photo via bongnews.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    if caption.startswith("!news2"):
        await _handle_news_media(update, context, caption, update.message.document)
        return
    await update.message.reply_text(
        "Use `!news2 content` caption to broadcast document via bongnews."
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    if caption.startswith("!news2"):
        await _handle_news_media(update, context, caption, update.message.video)
        return
    await update.message.reply_text("Use `!news2 content` caption to broadcast video via bongnews.")


# -- Bongnews commands --


async def cmd_bbnews_text(update: Update, content: str):
    """Handle !news text command."""
    content = content.replace("\\n", "\n")
    if not content:
        await update.message.reply_text(
            "Usage:\n• `!news content` — text only\n• `!news2 content` — with media",
            parse_mode="Markdown",
        )
        return
    await _broadcast_bbnews(update, content)


def _safe_media_filename(raw) -> str:
    """Reduce a client-supplied file name to a bare name with no path parts —
    a name like "..\\..\\x.txt" could otherwise place the download outside the target directory."""
    name = PureWindowsPath(str(raw or "")).name
    if not name or name in (".", ".."):
        name = f"media_{time.time_ns()}"
    return name


async def _handle_news_media(
    update: Update, context: ContextTypes.DEFAULT_TYPE, caption: str, media
):
    """Handle !news2 media — download from Telegram and broadcast to Discord."""
    content = caption[len("!news2") :].strip().replace("\\n", "\n")
    try:
        tg_file = await context.bot.get_file(media.file_id)
    except Exception as e:
        # Telegram refuses files over 20MB with a BadRequest, so reply with the
        # error rather than dropping it silently.
        log.warning(f"news2 get_file failed: {e}")
        await safe_reply(update.message, f"❌ 미디어 다운로드 실패: {e}", parse_mode=None)
        return

    fname = getattr(media, "file_name", None)
    if not fname:
        ext = ".mp4" if hasattr(media, "duration") else ".jpg"
        fname = f"media_{time.time_ns()}{ext}"
    # Private temp dir, not the BBS project root, which has no ignore rule for downloaded files.
    tmpdir = tempfile.mkdtemp(prefix=_TEMP_PREFIX)
    tmp = Path(tmpdir) / _safe_media_filename(fname)
    try:
        await tg_file.download_to_drive(str(tmp))
        file_bytes = tmp.read_bytes()
        await _broadcast_bbnews(update, content, file_bytes, tmp.name)
    except Exception as e:
        log.warning(f"news2 media broadcast failed: {e}")
        await safe_reply(update.message, f"❌ 미디어 발송 실패: {e}", parse_mode=None)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# -- Main message handler --


async def _dispatch_text(update: Update, context: ContextTypes.DEFAULT_TYPE, t: str):
    """Dispatch a text command using prefix table (longer/specific entries first)."""
    # Special cases that need local argument parsing
    if t.startswith("!newslist"):
        await _cns.cmd_newslist(update, context)
        return
    if t.startswith("!newscc"):
        await _cns.cmd_newscc(update, context)
        return
    if t.startswith("!news2"):
        await cmd_bbnews_text(update, t[len("!news2") :].strip())
        return
    if t.startswith("!news"):
        raw = t[len("!news") :].strip()
        try:
            scheduled_at, content = _cns.parse_schedule_time(raw)
            if not content:
                await update.message.reply_text("내용을 입력해주세요.")
                return
            news_id = await _cns.a_save_scheduled(content, scheduled_at)
            dt_kst = scheduled_at.astimezone(ZoneInfo("Asia/Seoul"))
            await safe_reply(
                update.message,
                f"✅ 예약됨 `[{news_id}]`\n"
                f"📅 {dt_kst.strftime('%m/%d(%a) %H:%M')} KST\n"
                f"📝 {content[:60]}",
                parse_mode="Markdown",
            )
        except sqlite3.OperationalError as e:
            # save_scheduled is deliberately single-attempt (retrying an
            # INSERT could double-schedule), so a locked DB must surface here, not fail silently.
            log.error(f"save_scheduled failed: {e}")
            await update.message.reply_text(
                "❌ DB 사용 중 — 뉴스가 예약되지 않았습니다. 잠시 후 다시 시도하세요."
            )
        except ValueError as e:
            if str(e) == _cns._NO_TIME:
                await cmd_bbnews_text(update, raw)
            else:
                await update.message.reply_text(
                    f"❌ {e}\n\n{_cns.TIME_FORMAT_HELP}", parse_mode="Markdown"
                )
        return
    # Exact first-token lookup: the command word is matched directly, so a short
    # command can no longer shadow a longer one that shares its prefix
    # (e.g. "!re0" is no longer intercepted by "!re").
    cmd = t.split(maxsplit=1)[0]
    handler = _CMD_TABLE.get(cmd)
    if handler is not None:
        await handler(update, context)
        return

    # Fallback for attached-argument forms with no space (e.g. "!log1", "!st7d"):
    # preserve the historical longest-prefix-first startswith behavior.
    for prefix, h in _CMD_ROUTES:
        if t.startswith(prefix):
            await h(update, context)
            return

    await update.message.reply_text("Unknown command. `!help` 참조.", parse_mode="Markdown")


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # PTB message filters key off effective_message, so edited_message,
    # channel_post and business_message updates reach this handler with
    # update.message None; check before the auth gate.
    message = update.message
    if message is None:
        return
    user = update.effective_user

    t = (message.text or "").strip()
    if t == "!chatid":
        chat = update.effective_chat
        await message.reply_text(
            f"chat_id: `{chat.id if chat else '?'}`\nuser_id: `{user.id if user else '?'}`",
            parse_mode="Markdown",
        )
        return
    # effective_user is None for channel posts, which must not pass the gate.
    if user is None or user.id != ALLOWED_USER_ID:
        await message.reply_text("No permission")
        return
    if message.photo:
        await handle_photo(update, context)
    elif message.video:
        await handle_video(update, context)
    elif message.document:
        await handle_document(update, context)
    else:
        await _dispatch_text(update, context, t)


# -- Startup --


# Prefix for the per-download temp directories, shared by the writer and the
# orphan sweeper so the two can never drift apart.
_TEMP_PREFIX = "_tg_news_"


def _cleanup_old_logs(days: int = 30):
    """Delete log files older than N days on startup."""
    cutoff = time.time() - days * 86400
    for f in LOG_DIR.glob("bridge_*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                log.info("Deleted old log: %s", f.name)
        except Exception as e:
            log.debug(f"cleanup failed: {e}")


def _cleanup_orphan_temp(max_age_sec: int = 3600):
    """Delete orphaned media-download temp dirs left behind by crashes — sweeps
    the system temp dir, and the BBS project root for older builds' leftovers."""
    cutoff = time.time() - max_age_sec

    def _remove_if_stale(entry: Path):
        try:
            if entry.stat().st_mtime >= cutoff:
                return
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            log.info("Deleted orphan temp: %s", entry.name)
        except Exception as e:
            log.debug(f"orphan cleanup failed: {e}")

    for entry in Path(tempfile.gettempdir()).glob(f"{_TEMP_PREFIX}*"):
        _remove_if_stale(entry)
    # Legacy location: the project tree, for older builds.
    for proj_dir in {Path(p) for p in PROJECTS.values()}:
        if not proj_dir.exists():
            continue
        for f in proj_dir.glob("_tg_*"):
            _remove_if_stale(f)


def _run_daily_maintenance():
    """Housekeeping the bridge owes once a day: log retention and a heartbeat —
    a healthy bridge can otherwise go days without a log line, making "alive" indistinguishable from "dead"."""
    try:
        _cleanup_old_logs()
    except Exception as e:
        log.warning(f"Scheduled log cleanup failed: {e}")
    try:
        _cleanup_orphan_temp()
    except Exception as e:
        log.warning(f"Scheduled temp cleanup failed: {e}")
    uptime_h = (time.monotonic() - _START_TIME) / 3600
    log.info("Bridge heartbeat: alive, uptime %.1fh", uptime_h)


async def _maintenance_task():
    """Background task: run _run_daily_maintenance once every 24h."""
    while True:
        await asyncio.sleep(_MAINTENANCE_INTERVAL_SEC)
        try:
            _run_daily_maintenance()
        except Exception as e:
            log.error(f"Maintenance task error: {e}")


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists.

    No os.kill(pid, 0) on Windows (TerminateProcess); exit code 259 is indistinguishable from STILL_ACTIVE.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        WAIT_TIMEOUT = 0x00000102
        ERROR_ACCESS_DENIED = 5
        ERROR_INVALID_PARAMETER = 87

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            # A failed open is not proof of death: Windows reports a running
            # process this account may not touch as ACCESS_DENIED; only INVALID_PARAMETER means "no such pid".
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                return True
            if err != ERROR_INVALID_PARAMETER:
                log.debug("OpenProcess(%s) failed with error %s", pid, err)
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# Substring that identifies this bridge in a process command line.
_BRIDGE_MARKER = "telegram_bridge.py"


def _process_command_line(pid: int):
    """Command line of a running process, or None when it cannot be read.

    None means "could not determine", never "not running" — callers must fail
    open. Shells out to CIM (no cheap ctypes route); the pid travels via env var so nothing is interpolated into the query.
    """
    if sys.platform == "win32":
        script = (
            "$p = Get-CimInstance Win32_Process "
            '-Filter "ProcessId=$env:BRIDGE_PROBE_PID" -ErrorAction SilentlyContinue; '
            "if ($null -eq $p) { 'NOPROC' } "
            # A CommandLine of $null means the process exists but won't say what
            # it is (protected/cross-session) — "cannot tell", not "not running".
            "elseif ($null -eq $p.CommandLine) { 'UNKNOWN' } "
            "else { 'CMD:' + $p.CommandLine }"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=20,
                env={**os.environ, "BRIDGE_PROBE_PID": str(pid)},
            )
        except Exception as e:
            log.warning("Could not read command line for pid %s: %s", pid, e)
            return None
        out = (result.stdout or "").strip()
        if result.returncode != 0 or not out:
            return None
        if out == "NOPROC":
            return ""
        if out == "UNKNOWN":
            return None
        return out[len("CMD:") :] if out.startswith("CMD:") else None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return None


def _lock_holder_state(pid: int) -> str:
    """Classify the pid recorded in the lock file.

    "gone"    - no such process, the file is stale.
    "bridge"  - a running telegram_bridge; this start must stand down.
    "other"   - a running process that is not the bridge; the pid was recycled.
    "unknown" - the probe itself failed; the caller must fail open.
    """
    if pid <= 0:
        return "gone"
    if not _pid_alive(pid):
        return "gone"
    cmdline = _process_command_line(pid)
    if cmdline is None:
        return "unknown"
    if not cmdline:
        return "gone"
    return "bridge" if _BRIDGE_MARKER in cmdline else "other"


def acquire_single_instance(pid_path: Path = None) -> bool:
    """Claim the bridge pid file. False means a live bridge already holds it.

    When unsure, start; only a pid whose command line names this script blocks.
    """
    pid_path = pid_path or (LOG_DIR / "bridge.pid")
    try:
        raw = pid_path.read_text(encoding="utf-8").strip() if pid_path.exists() else ""
    except Exception as e:
        log.warning("Pid file %s unreadable (%s); treating it as stale.", pid_path, e)
        raw = ""

    # isascii() as well as isdigit(): the latter is True for Unicode digit forms
    # that int() then rejects, and an exception here would abort the start.
    holder = None
    if raw.isascii() and raw.isdigit():
        try:
            holder = int(raw)
        except ValueError:
            holder = None
    if holder is None:
        if raw:
            log.warning("Pid file %s holds %r, not a pid; treating it as stale.", pid_path, raw)
    elif holder != os.getpid():
        state = _lock_holder_state(holder)
        if state == "bridge":
            log.error("Another bridge instance is already running (pid %s); exiting.", holder)
            return False
        if state == "other":
            log.warning("Pid %s belongs to an unrelated process; claiming the lock.", holder)
        elif state == "unknown":
            log.warning("Could not verify pid %s; starting anyway.", holder)

    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        log.warning("Could not write pid file %s: %s", pid_path, e)
    return True


def release_single_instance(pid_path: Path = None) -> None:
    """Delete our pid file, but only while it still names this process —
    deleting one another instance has since claimed would hand a second poller the lock."""
    pid_path = pid_path or (LOG_DIR / "bridge.pid")
    try:
        if not pid_path.exists():
            return
        if pid_path.read_text(encoding="utf-8").strip() != str(os.getpid()):
            return
        pid_path.unlink()
    except Exception as e:
        log.debug("Could not remove pid file %s: %s", pid_path, e)


_START_TIME = time.monotonic()
_MAINTENANCE_INTERVAL_SEC = 24 * 3600

# Strong refs to the background tasks: asyncio holds tasks weakly, so an
# unreferenced create_task result can be garbage collected mid-run — the exact
# "bridge goes silent" failure class this repo has hit before.
_bg_tasks: list = []


async def post_init(application):
    _cleanup_old_logs()
    _cleanup_orphan_temp()
    check_shared_schema()
    _bg_tasks.append(asyncio.create_task(_loop_monitor_task(application)))
    _bg_tasks.append(asyncio.create_task(_news_scheduler_task(application)))
    _bg_tasks.append(asyncio.create_task(_maintenance_task()))
    if BBS_DB_PATH.exists():
        await _check_boot_scheduled_news(application)
    else:
        # A standalone clone has no sibling BBS repo; connecting anyway would
        # raise inside post_init and kill the bot before polling ever starts.
        log.warning("bbs.db not found - boot scheduled-news check skipped")
    log.info("Telegram bridge starting")


import cmd_help as _cc  # noqa: E402
import cmd_discord as _cd  # noqa: E402
import cmd_subscribers as _cs  # noqa: E402
import cmd_moderation as _cm  # noqa: E402

# Dispatch table for text commands — longer/more-specific prefixes must come first
# to prevent shorter prefixes from shadowing them (e.g. "!re0" before "!re").
_CMD_ROUTES: list[tuple[str, object]] = [
    ("!reboot", _cm.cmd_reboot),
    # Typo guard: "!rebot"/"!reboo" must reach the reboot usage text, not fall through to "!re".
    ("!reb", _cm.cmd_reboot),
    ("!re0", _cm.cmd_restart),
    ("!re", _cd.cmd_drestart),
    ("!sync", _cd.cmd_sync),
    ("!list", _cs.cmd_list),
    ("!help", _cc.cmd_help),
    ("!st", _cm.cmd_stats),
    ("!dm", _cd.cmd_dm),
    ("!hp", _cd.cmd_hp),
    ("!log", _cd.cmd_log),
    ("!ud", _cd.cmd_dm_log),
    ("!sub", _cs.cmd_sub),
    ("!se", _cs.cmd_sub_events),
]

# Exact-match table built from the ordered routes above. The list is kept as the
# fallback source of truth for attached-argument (spaceless) command forms.
_CMD_TABLE: dict[str, object] = dict(_CMD_ROUTES)


async def _cmd_help_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cc.cmd_help(update, context)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Quiet transient polling errors; full traceback only for real bugs."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        # Telegram-side hiccup (Bad Gateway, timeout) — PTB auto-retries.
        log.warning(f"Transient network error (auto-retry): {err}")
        return
    log.error("Unhandled exception in handler", exc_info=err)


_req = HTTPXRequest(
    connect_timeout=20.0,
    read_timeout=20.0,
    write_timeout=20.0,
    httpx_kwargs={
        "transport": httpx.AsyncHTTPTransport(retries=2, local_address="0.0.0.0"),
    },
)
app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .request(_req)
    .get_updates_request(_req)
    .post_init(post_init)
    .build()
)
app.add_error_handler(_on_error)
app.add_handler(CommandHandler("help", _cmd_help_entry))
app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.VIDEO | filters.Document.ALL,
        handle,
    )
)
if __name__ == "__main__":
    if not acquire_single_instance():
        print("another telegram bridge instance is already running; exiting.")
        sys.exit(1)
    # Both paths: the finally covers a normal return and an exception, atexit
    # covers interpreter shutdown routes that skip it (sys.exit from a handler).
    atexit.register(release_single_instance)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    print("telegram bridge running...")
    try:
        app.run_polling()
    finally:
        release_single_instance()
