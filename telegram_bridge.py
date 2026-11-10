"""Telegram bridge — thin hub that wires handlers and starts polling.

Split modules:
  tg_config.py      — env, paths, constants, mutable globals
  tg_utils.py       — message splitting, HUD, formatting helpers
  discord_helpers.py — Discord API helpers, username cache, DM, bongnews
"""

import asyncio
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

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
    BBS_DOWN_THRESHOLD_MIN,
    BOT_TOKEN,
    LOG_DIR,
    LOOP_ALERT_COOLDOWN_SEC,
    LOOP_ALERT_STATE_PATH,
    RESTART_MARKER,
    check_shared_schema,
    fetch_rows_async,
    log,
    loop_threshold_min,
    redact_tokens,
    refresh_loop_thresholds,
)
from tg_utils import ensure_utc, is_authorized, safe_reply, safe_send
from discord_helpers import _broadcast_bbnews


# -- Loop monitor --

# State key for the whole-bot rule; loop ids all end in "_loop", so it cannot collide.
_BBS_DOWN_KEY = "__bbs_down__"


def _load_alert_state() -> dict[str, float]:
    """{key: last alert epoch} for every key currently alerted as stale."""
    try:
        data = json.loads(LOOP_ALERT_STATE_PATH.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f"Loop alert state unreadable, starting empty: {e}")
        return {}


def _save_alert_state(state: dict[str, float]) -> None:
    try:
        tmp = LOOP_ALERT_STATE_PATH.with_name(LOOP_ALERT_STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, LOOP_ALERT_STATE_PATH)
    except Exception as e:
        log.warning(f"Loop alert state write failed: {e}")


def _fmt_age(mins: float) -> str:
    h, m = int(mins // 60), int(mins % 60)
    return f"{h}h{m}m" if h else f"{m}m"


def _cooldown_over(state: dict[str, float], key: str, now_ts: float) -> bool:
    return key not in state or now_ts - state[key] > LOOP_ALERT_COOLDOWN_SEC


def _evaluate_loops(ages: dict[str, float], state: dict[str, float], now_ts: float):
    """Return (alerts, recoveries) for loop ages in minutes; updates *state* in place.

    While the whole bot is down only that one alert is raised, not one per loop.
    """
    alerts, recoveries = [], []
    if not ages:
        return alerts, recoveries
    newest = min(ages.values())
    if newest > BBS_DOWN_THRESHOLD_MIN:
        if _cooldown_over(state, _BBS_DOWN_KEY, now_ts):
            state[_BBS_DOWN_KEY] = now_ts
            alerts.append(f"🛑 BBS bot down — no loop has run for {_fmt_age(newest)}")
        return alerts, recoveries
    if state.pop(_BBS_DOWN_KEY, None) is not None:
        recoveries.append("✅ BBS bot loops running again")
    for loop_id, mins in sorted(ages.items()):
        if mins > loop_threshold_min(loop_id):
            if _cooldown_over(state, loop_id, now_ts):
                state[loop_id] = now_ts
                alerts.append(f"⚠️ `{loop_id}` — {_fmt_age(mins)} no response")
        elif state.pop(loop_id, None) is not None:
            recoveries.append(f"✅ `{loop_id}` recovered")
    return alerts, recoveries


async def _loop_monitor_task(app):
    """Background task: alert on stale loops or a down bot, and again on recovery."""
    state = _load_alert_state()
    await asyncio.sleep(60)
    while True:
        await asyncio.sleep(300)
        if not BBS_DB_PATH.exists():
            continue
        try:
            now_utc = datetime.now(timezone.utc)
            rows = await fetch_rows_async("SELECT loop_id, last_run FROM loop_health")
            ages = {
                row["loop_id"]: (
                    now_utc - ensure_utc(datetime.fromisoformat(row["last_run"]))
                ).total_seconds()
                / 60
                for row in rows
            }
            await asyncio.to_thread(refresh_loop_thresholds)
            before = dict(state)
            alerts, recoveries = _evaluate_loops(ages, state, time.time())
            if state != before:
                await asyncio.to_thread(_save_alert_state, dict(state))
            if alerts:
                await safe_send(
                    app.bot, ALLOWED_USER_ID, "Loop down detected\n\n" + "\n".join(alerts)
                )
                log.warning(f"Loop alert: {alerts}")
            if recoveries:
                await safe_send(app.bot, ALLOWED_USER_ID, "\n".join(recoveries))
                log.info(f"Loop recovered: {recoveries}")
        except Exception as e:
            log.error(f"Loop monitor error: {e}")


# -- Photo / video / document handlers --


def _is_news2(caption: str) -> bool:
    """Exact command word, so "!news2x ..." never broadcasts."""
    return caption.split(maxsplit=1)[:1] == ["!news2"]


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    if _is_news2(caption):
        await _handle_news_media(update, context, caption, update.message.photo[-1])
        return
    await update.message.reply_text("Use `!news2 content` caption to broadcast photo via bongnews.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    if _is_news2(caption):
        await _handle_news_media(update, context, caption, update.message.document)
        return
    await update.message.reply_text(
        "Use `!news2 content` caption to broadcast document via bongnews."
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    if _is_news2(caption):
        await _handle_news_media(update, context, caption, update.message.video)
        return
    await update.message.reply_text("Use `!news2 content` caption to broadcast video via bongnews.")


# -- Bongnews commands --


_NEWS_USAGE = "Usage:\n• `!news content` — text only\n• `!news2 content` — with media"


async def cmd_bbnews_text(update: Update, content: str):
    """Handle !news text command: broadcast now, exactly as written."""
    content = content.replace("\\n", "\n")
    if not content.strip():
        await update.message.reply_text(_NEWS_USAGE, parse_mode="Markdown")
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
        await safe_reply(
            update.message, f"❌ 미디어 다운로드 실패: {redact_tokens(str(e))}", parse_mode=None
        )
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
        await safe_reply(
            update.message, f"❌ 미디어 발송 실패: {redact_tokens(str(e))}", parse_mode=None
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# -- Main message handler --


async def _dispatch_text(update: Update, context: ContextTypes.DEFAULT_TYPE, t: str):
    """Dispatch a text command by its exact first token."""
    parts = t.split(maxsplit=1)
    cmd = parts[0] if parts else ""
    # Log only a known command name: attached forms like "!ud<uid>" carry user ids.
    known = cmd if cmd in ("!news", "!news2") or cmd in _CMD_TABLE else None
    if known is None:
        known = next((p for p in _ATTACHED_ARG_PREFIXES if cmd.startswith(p)), "unknown")
    log.info("command: %s", known)
    if cmd == "!news":
        await cmd_bbnews_text(update, parts[1] if len(parts) > 1 else "")
        return
    if cmd == "!news2":
        # Media captions are handled by handle_photo/video/document; text alone sends nothing.
        await update.message.reply_text(_NEWS_USAGE, parse_mode="Markdown")
        return
    handler = _CMD_TABLE.get(cmd)
    if handler is not None:
        await handler(update, context)
        return

    # Attached-argument forms with no space (e.g. "!log1", "!st7d"). Only commands
    # that parse such an argument; "!re" must stay exact or "!res" restarts the bot.
    for prefix in _ATTACHED_ARG_PREFIXES:
        if cmd.startswith(prefix):
            await _CMD_TABLE[prefix](update, context)
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
    if not is_authorized(update):
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
    """Delete orphaned media-download temp dirs left behind by crashes in the system temp dir."""
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
    _bg_tasks.append(asyncio.create_task(_maintenance_task()))
    await _report_restart_done(application)
    log.info("Telegram bridge starting")


async def _report_restart_done(application):
    # Fail-open: a missing or unreadable marker must never block startup.
    try:
        # Atomic claim: only one of two overlapping boots can win the rename.
        claimed = RESTART_MARKER.with_name(f"{RESTART_MARKER.name}.{os.getpid()}")
        try:
            os.replace(RESTART_MARKER, claimed)
        except FileNotFoundError:
            return
        try:
            raw = claimed.read_text(encoding="utf-8").strip()
        finally:
            claimed.unlink(missing_ok=True)
        try:
            elapsed = int(time.time() - float(raw))
        except ValueError:
            elapsed = -1
        if 0 <= elapsed <= 300:
            text = f"✅ 텔레그램 봇 재시작 완료 ({elapsed}초)"
        elif elapsed > 300:
            text = f"✅ 텔레그램 봇 시작됨 (마지막 !re0 요청 {elapsed // 60}분 전)"
        else:
            text = "✅ 텔레그램 봇 재시작 완료"
        await safe_send(application.bot, ALLOWED_USER_ID, text)
    except Exception as e:
        log.warning(f"Restart completion notice failed: {e}")


import cmd_help as _cc  # noqa: E402
import cmd_discord as _cd  # noqa: E402
import cmd_subscribers as _cs  # noqa: E402
import cmd_moderation as _cm  # noqa: E402

# Text commands, matched on the exact first token.
_CMD_ROUTES: list[tuple[str, object]] = [
    ("!reboot", _cm.cmd_reboot),
    ("!re0", _cm.cmd_restart),
    ("!re", _cd.cmd_drestart),
    ("!list", _cs.cmd_list),
    ("!help", _cc.cmd_help),
    ("!st", _cm.cmd_stats),
    ("!hp", _cd.cmd_hp),
    ("!log", _cd.cmd_log),
    ("!ud", _cd.cmd_dm_log),
    ("!sub", _cs.cmd_sub),
    ("!se", _cs.cmd_sub_events),
]

_CMD_TABLE: dict[str, object] = dict(_CMD_ROUTES)

# Handlers that slice their argument off text[len(prefix):], so "!st7d" works unspaced.
_ATTACHED_ARG_PREFIXES = ("!log", "!st", "!se", "!ud")


async def _cmd_help_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cc.cmd_help(update, context)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Quiet transient polling errors; report every handler failure to the operator."""
    err = context.error
    # PTB passes update=None for getUpdates errors, which it retries by itself.
    if update is None and isinstance(err, (NetworkError, TimedOut)):
        log.warning(f"Transient network error (auto-retry): {err}")
        return
    log.error("Unhandled exception in handler", exc_info=err)
    # Only the operator's own commands page; a stranger's message failing is log-only noise.
    if update is None or not (isinstance(update, Update) and is_authorized(update)):
        return
    # A handler that timed out may already have acted (a broadcast may have gone out).
    await safe_send(
        context.bot,
        ALLOWED_USER_ID,
        f"❌ 명령 처리 실패 ({type(err).__name__}): {redact_tokens(str(err))}\n"
        "발송 명령이었다면 다시 보내기 전에 결과를 확인하세요.",
        parse_mode=None,
    )


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
        # start_bot.bat ends its restart loop on this exit code.
        sys.exit(3)
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
