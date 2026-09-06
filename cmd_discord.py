"""Commands: Discord bot management (!re, !dm, !ud, !log, !hp)."""

import asyncio
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from telegram import Update
from telegram.ext import ContextTypes

import tg_config as _cfg
import tg_utils as _utils
import discord_helpers as _dh
from tg_config import KST as _KST
from tg_utils import admin_only, _to_kst, ensure_utc, md_escape, parse_user_id


# The BBS bot logs through TimedRotatingFileHandler with the default namer:
# the live file keeps its base name and each rotated day gets a .YYYY-MM-DD
# suffix. Both helpers below used to encode a scheme that never existed.
_MAX_LOG_DAYS = 365

# Seam for the post-restart settle wait. Tests replace this rather than
# asyncio.sleep, which they would otherwise swap out process-wide.
_sleep = asyncio.sleep


def _bot_log_path(logs_dir):
    """Path of the BBS bot's current info log.

    The old bot_<date>.txt guess never matched a real file, so the post-restart
    error check silently did nothing on every restart since it was written.
    """
    return logs_dir / "bot.log"


def _error_log_path(logs_dir, days_ago: int, target_date: str):
    """Path of the BBS error log for a given age in days.

    Today's errors are still in error.log; older days rotate to
    error.log.YYYY-MM-DD. The old error_<date>.log guess matched nothing, so
    !log answered "No error log" every single time it was run.
    """
    return logs_dir / ("error.log" if days_ago == 0 else f"error.log.{target_date}")


@admin_only
async def cmd_drestart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!re — restart the Discord bot, then check log for errors."""
    _cfg.log.info("Discord bot restart requested")
    await update.message.reply_text("🔄 Discord 봇 재시작 중...")
    discord_path = _cfg.PROJECTS["discord"]
    try:
        # The bot root travels in an environment variable instead of being
        # interpolated into the script text, so nothing in the path can
        # terminate a PowerShell string. $rootPrefix also pins every match to
        # that exact directory: the old '*<root>*' pattern matched sibling
        # checkouts such as BBS-SCV and bbs-web too.
        ps_command = (
            "$root = $env:BBS_RESTART_ROOT; "
            # Without a root every -like pattern collapses to '**' and the
            # script would sweep unrelated python processes; refuse instead.
            "if (-not $root) { 'no-root'; exit 1 }; "
            "$rootPrefix = $root.TrimEnd('\\') + '\\'; "
            # [ and ] are wildcards to -like; escape them so the filter
            # cannot widen past this exact directory.
            "$likePrefix = [WildcardPattern]::Escape($rootPrefix); "
            "$cmd = Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -like ('*' + $likePrefix + 'run.bat*') }; "
            "$py = Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*bot.py*' -and $_.CommandLine -like ('*' + $likePrefix + '*') }; "
            "$killed = @(); "
            "$pids = @(); "
            "foreach ($p in @($cmd) + @($py)) { if ($p) { $pids += $p.ProcessId; Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; $killed += [string]$p.ProcessId } }; "
            "if ($pids.Count -gt 0) { "
            "  $deadline = (Get-Date).AddSeconds(5); "
            "  do { Start-Sleep -Milliseconds 300; $still = @($pids | Where-Object { (Get-Process -Id $_ -ErrorAction SilentlyContinue) -ne $null }) } while ($still.Count -gt 0 -and (Get-Date) -lt $deadline) "
            "}; "
            "$proc = Start-Process -WindowStyle Minimized -FilePath 'cmd.exe' -ArgumentList '/c', ('\"' + $rootPrefix + 'run.bat\"') -WorkingDirectory $root -PassThru; "
            "Start-Sleep -Milliseconds 800; "
            "if ($proc -and -not $proc.HasExited) { $killed += 'started' } else { $killed += 'spawn-failed' }; "
            "$killed -join ','"
        )
        result = await asyncio.to_thread(
            subprocess.run,
            ["powershell", "-NoProfile", "-Command", ps_command],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "BBS_RESTART_ROOT": discord_path},
        )
        killed = result.stdout.strip()
        err = result.stderr.strip()
        if killed == "no-root":
            _cfg.log.error("Discord restart aborted: BBS_RESTART_ROOT was empty")
            await update.message.reply_text(
                "Restart aborted: the Discord bot root path is not configured."
            )
        elif "spawn-failed" in killed:
            # New process image exited immediately — treat as a failed spawn.
            _cfg.log.error(f"Discord restart: new process failed to start stderr={err}")
            await update.message.reply_text(
                "❌ 새 Discord 봇 프로세스가 시작 직후 종료됐습니다. run.bat 확인 필요."
            )
        elif killed:
            _cfg.log.info(f"Discord restart processed: {killed}")
            await update.message.reply_text(f"✅ Discord 재시작 처리 완료: {killed}")
        else:
            _cfg.log.warning(f"Discord restart: no target found stderr={err}")
            await update.message.reply_text(
                "⚠️ 재시작 대상 프로세스를 찾지 못했고 새 run.bat도 시작하지 못했습니다."
            )
    except Exception as e:
        await update.message.reply_text(f"❌ 재시작 실패: {e}")
        _cfg.log.error(f"Discord bot restart failed: {e}")
        return

    # Check the latest Discord bot log for errors after a short delay
    await _sleep(6)
    try:
        log_file = _bot_log_path(_cfg.DISCORD_LOGS_PATH)
        if log_file.exists():
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-10:] if len(lines) >= 10 else lines
            # Line-anchored match: only real error markers, not benign lines that
            # merely contain the substring "Error" (e.g. "0 errors", "ErrorLevel").
            has_error = any(
                "[ERROR]" in ln
                or ln.lstrip().startswith("Traceback")
                or ln.lstrip().startswith("SyntaxError")
                for ln in tail
            )
            if has_error:
                await update.message.reply_text(
                    "⚠️ *재시작 후 오류 감지*\n```\n" + "\n".join(tail[-5:]) + "\n```",
                    parse_mode="Markdown",
                )
    except Exception as e:
        _cfg.log.warning(f"Post-restart log check failed: {e}")


@admin_only
async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!sync — Write sync flag to BBS dir and restart Discord bot to sync slash commands."""
    discord_path = Path(_cfg.PROJECTS["discord"])
    flag = discord_path / ".sync_flag"
    try:
        flag.write_text("sync", encoding="utf-8")
        _cfg.log.info("sync flag written: %s", flag)
    except Exception as e:
        await update.message.reply_text(f"❌ sync 플래그 생성 실패: {e}")
        return
    await update.message.reply_text("🔄 sync 플래그 설정. Discord 봇 재시작 중...")
    await cmd_drestart.__wrapped__(update, context)


@admin_only
async def cmd_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!dm <userID> <message> — send a Discord DM to a specific user."""
    text = (update.message.text or "").strip()
    rest = text[len("!dm") :].strip()
    parts = rest.split(" ", 1)
    user_id = parse_user_id(parts[0]) if parts else None
    if len(parts) < 2 or user_id is None:
        await update.message.reply_text(
            "사용법: `!dm <userID> <message>`\n예시: `!dm 123456789012345678 안녕하세요`",
            parse_mode="Markdown",
        )
        return

    message = parts[1].strip()
    if not message:
        await update.message.reply_text("❌ 메시지 내용을 입력해주세요.")
        return

    token = await _dh._get_discord_token_async()
    if not token:
        await update.message.reply_text("❌ Discord 토큰을 찾을 수 없어요.")
        return

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            ok = await _dh._send_discord_dm(session, token, user_id, message)
        if ok:
            await update.message.reply_text(f"✅ DM 발송 완료 → `{user_id}`", parse_mode="Markdown")
            _cfg.log.info(f"DM sent to {user_id}")
        else:
            await update.message.reply_text(f"❌ DM 발송 실패 → `{user_id}`", parse_mode="Markdown")
            _cfg.log.warning(f"DM failed to {user_id}")
    except Exception as e:
        await update.message.reply_text(f"❌ 오류: {e}")
        _cfg.log.error(f"cmd_dm error: {e}")


@admin_only
async def cmd_dm_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!ud [userID] — View stored user DMs. No arg = summary, with arg = messages."""
    # Opened on the event loop. Bounded exposure: these are plain SELECTs
    # with no retry sleep, so the worst stall is get_bbs_db's 4s busy
    # timeout. Every write, and the multi-query !list/!hp/!newscc paths, go
    # through db_write_async/db_read_async instead.
    conn = _cfg.open_db()
    if conn is None:
        await update.message.reply_text("DB not found.")
        return

    text = (update.message.text or "").strip()
    arg = text[len("!ud") :].strip()
    uid = parse_user_id(arg) if arg else None

    try:
        if uid is not None:
            rows = conn.execute(
                "SELECT content, created_at FROM user_dm"
                " WHERE user_id = ? ORDER BY created_at DESC LIMIT 20",
                (uid,),
            ).fetchall()
            # Release the shared DB before any HTTP await: this handler used to
            # keep the connection open across a 15s Discord username lookup.
            conn.close()
            if not rows:
                await update.message.reply_text(f"No DMs from {uid}")
                return
            names = await _dh._fetch_discord_usernames([str(uid)])
            name = md_escape(names.get(str(uid), str(uid)))
            lines = [f"*{name}* (`{uid}`) DM ({len(rows)})\n"]
            for r in rows:
                ts = _to_kst(r["created_at"])
                content = r["content"][:80]
                lines.append(f"`{ts}` {content}")
            await _utils.safe_reply(update.message, "\n".join(lines))
        else:
            rows = conn.execute(
                "SELECT user_id, COUNT(*) as cnt, MAX(created_at) as last_at"
                " FROM user_dm GROUP BY user_id ORDER BY last_at DESC"
            ).fetchall()
            conn.close()
            if not rows:
                await update.message.reply_text("No DMs stored.")
                return
            all_uids = [str(r["user_id"]) for r in rows]
            names = await _dh._fetch_discord_usernames(all_uids)
            lines = ["*User DM Log*\n"]
            for r in rows:
                uid_str = str(r["user_id"])
                name = md_escape(names.get(uid_str, uid_str))
                last = _to_kst(r["last_at"]) if r["last_at"] else "?"
                lines.append(f"*{name}* (`{uid_str}`) — {r['cnt']}건 (last: {last})")
            await _utils.safe_reply(update.message, "\n".join(lines))
    finally:
        conn.close()


@admin_only
async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!log [N] — Show Discord bot error log. N = days ago (default 0 = today)."""
    text = (update.message.text or "").strip()
    arg = text[len("!log") :].strip()

    m = re.match(r"^(\d+)d?$", arg)
    # An unbounded N made timedelta raise OverflowError; retention is 30 days,
    # so anything past a year is a typo either way.
    days_ago = min(int(m.group(1)), _MAX_LOG_DAYS) if m else 0

    target_date = (datetime.now(_KST) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    err_file = _error_log_path(_cfg.DISCORD_LOGS_PATH, days_ago, target_date)
    label = "오늘" if days_ago == 0 else f"{days_ago}일 전 ({target_date})"

    if not err_file.exists():
        await _utils.safe_reply(update.message, f"No error log for {target_date}")
        return

    try:
        raw = err_file.read_text(encoding="utf-8")
    except Exception:
        raw = ""

    if not raw.strip():
        await _utils.safe_reply(update.message, f"No errors {label}")
        return

    tail = raw[-2000:] if len(raw) > 2000 else raw
    lines = tail.strip().split("\n")
    total = raw.count("[ERROR]")
    header = f"*Error Log* ({label}) — {total} error(s)\n\n"
    body = "\n".join(f"`{ln[:120]}`" for ln in lines[-20:])
    await _utils.safe_reply(update.message, header + body)


@admin_only
async def cmd_hp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!hp — Discord bot loop health from SQLite loop_health table."""
    if not _cfg.BBS_DB_PATH.exists():
        await update.message.reply_text("DB not found.")
        return

    now_kst = datetime.now(_KST)
    dates = [(now_kst - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]

    def _read(conn):
        health = {
            r["loop_id"]: r
            for r in conn.execute("SELECT loop_id, last_run FROM loop_health").fetchall()
        }
        daily_rows = conn.execute(
            "SELECT loop_id, date, run_count FROM loop_health_daily WHERE date IN (?,?,?)",
            dates,
        ).fetchall()
        return health, daily_rows

    # Off the event loop: opening the shared bbs.db blocks for up to the 4s busy
    # timeout whenever the Discord bot is mid-write.
    health, daily_rows = await _cfg.db_read_async(_read)

    daily: dict[str, dict[str, int]] = {}
    for r in daily_rows:
        daily.setdefault(r["loop_id"], {})[r["date"]] = r["run_count"]

    loop_ids = sorted(health.keys())
    now_utc = datetime.now(timezone.utc)
    header = f"🔍 *Loop Health* — {now_kst.strftime('%m/%d %H:%M')} KST\n"
    header += f"`{'Loop':<16} {'마지막':>8}  {'D':>5} {'D-1':>5} {'D-2':>5}`\n"
    lines = [header]

    for loop_id in loop_ids:
        desc = loop_id.removesuffix("_loop")
        row = health.get(loop_id)
        if row:
            last_dt = ensure_utc(datetime.fromisoformat(row["last_run"]))
            diff = now_utc - last_dt
            mins = int(diff.total_seconds() // 60)
            age = f"{mins}분" if mins < 60 else f"{mins // 60}h{mins % 60}m"
            d0 = daily.get(loop_id, {}).get(dates[0], 0)
            d1 = daily.get(loop_id, {}).get(dates[1], 0)
            d2 = daily.get(loop_id, {}).get(dates[2], 0)
            lines.append(f"`{'✅ ' + desc:<16} {age:>8}  {d0:>5} {d1:>5} {d2:>5}`")
        else:
            lines.append(f"`{'⚠️ ' + desc:<16} {'없음':>8}  {'-':>5} {'-':>5} {'-':>5}`")

    await _utils.safe_reply(update.message, "\n".join(lines))
