"""Commands: bridge restart, machine reboot and usage stats (!re0, !reboot, !st)."""

import asyncio
import re
import subprocess
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

import tg_config as _cfg
import tg_utils as _utils
import discord_helpers as _dh
from tg_utils import admin_only, md_escape, parse_user_id


# Holds the pending !re0 restart task so GC can't collect it pre-fire.
_restart_task = None


@admin_only
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!re0 — restart the Telegram bot.

    Relaunches via start_bot.bat (detached) instead of os.execv: the bat's
    kill-filter sweeps ALL existing bridge instances first, so duplicates
    can never survive a restart (execv only replaced this process image and
    kept any stray second instance polling -> telegram.error.Conflict).
    """
    _cfg.log.info("Telegram bot restart requested")
    await update.message.reply_text("✅ 텔레그램 봇 재시작 중..")

    async def _do_restart():
        await asyncio.sleep(1)
        bat = Path(__file__).parent / "start_bot.bat"
        subprocess.Popen(
            ["cmd", "/c", "start", "", "/min", str(bat)],
            cwd=str(bat.parent),
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        # The spawned bat kills this process within ~2s; exit is handled there.

    global _restart_task
    _restart_task = asyncio.create_task(_do_restart())  # ref: survive GC until it fires


# Grace period before the OS reboots. The Telegram request stack can spend up
# to 20s on one send (see telegram_bridge._req), and "!reboot cc" needs a phone round trip.
_REBOOT_DELAY_SEC = 60

# Win32 error codes shutdown.exe exits with.
_ERR_SHUTDOWN_ALREADY_SCHEDULED = 1190
_ERR_NO_SHUTDOWN_IN_PROGRESS = 1116

_REBOOT_USAGE = (
    "⚠️ 이 PC를 재부팅합니다. 실행하려면 `!reboot ok`, 예약 취소는 `!reboot cc`.\n"
    "재부팅 후 로그인 전까지 봇 2개 모두 꺼져 있음 — 크롬 원격으로 로그인하면 시작프로그램이 다시 띄움."
)


async def _run_shutdown(args: list[str]):
    # "oem": console tools write the OEM code page regardless of Python's locale or UTF-8 mode.
    return await asyncio.to_thread(
        subprocess.run,
        ["shutdown", *args],
        capture_output=True,
        encoding="oem",
        errors="replace",
        timeout=15,
    )


def _shutdown_output(result) -> str:
    return result.stderr.strip() or result.stdout.strip()


@admin_only
async def cmd_reboot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!reboot ok — reboot this machine after a short delay; !reboot cc — abort."""
    parts = (update.message.text or "").split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    # Typos routed here by the "!reb" guard must never reboot; only the exact word counts.
    if parts[0] != "!reboot" or arg not in ("ok", "cc"):
        await _utils.safe_reply(update.message, _REBOOT_USAGE)
        return

    try:
        if arg == "cc":
            result = await _run_shutdown(["/a"])
        else:
            _cfg.log.warning("Machine reboot requested via !reboot")
            result = await _run_shutdown(
                ["/r", "/f", "/t", str(_REBOOT_DELAY_SEC), "/d", "p:0:0", "/c", "BBS-SCV !reboot"]
            )
    except Exception as e:
        _cfg.log.error(f"shutdown.exe failed to run: {e}")
        await update.message.reply_text(f"❌ shutdown 실행 실패: {e}")
        return

    rc = result.returncode
    if arg == "cc":
        if rc == 0:
            _cfg.log.info("Machine reboot aborted")
            await update.message.reply_text("✅ 재부팅 예약 취소됨")
        elif rc == _ERR_NO_SHUTDOWN_IN_PROGRESS:
            await update.message.reply_text("ℹ️ 예약된 재부팅 없음")
        else:
            await update.message.reply_text(f"❌ 취소 실패 ({rc}): {_shutdown_output(result)}")
        return

    if rc == 0:
        await _utils.safe_reply(
            update.message,
            f"🔁 {_REBOOT_DELAY_SEC}초 후 PC 재부팅. 취소: `!reboot cc`\n"
            "재부팅 후 크롬 원격으로 로그인해야 봇이 다시 뜸.",
        )
    elif rc == _ERR_SHUTDOWN_ALREADY_SCHEDULED:
        await _utils.safe_reply(update.message, "⏳ 이미 재부팅 카운트다운 중. 취소: `!reboot cc`")
    else:
        _cfg.log.error(f"shutdown /r failed rc={rc} stderr={result.stderr.strip()}")
        await update.message.reply_text(f"❌ 재부팅 명령 실패 ({rc}): {_shutdown_output(result)}")


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!st [userID] — command usage stats from SQLite. No arg = summary, with arg = user detail."""
    text = (update.message.text or "").strip()
    raw_arg = text[len("!st") :].strip()

    period_match = re.search(r"(\d+)d", raw_arg)
    days_filter = int(period_match.group(1)) if period_match else None
    arg = re.sub(r"\d+d", "", raw_arg).strip()

    if days_filter:
        time_clause = " AND used_at > datetime('now', ? || ' days')"
        time_params: tuple = (f"-{days_filter}",)
        period_label = f" (최근 {days_filter}일)"
    else:
        time_clause = ""
        time_params = ()
        period_label = ""

    if not _cfg.BBS_DB_PATH.exists():
        await update.message.reply_text("DB not found.")
        return

    uid = parse_user_id(arg)
    if uid is not None:
        rows = await _cfg.fetch_rows_async(
            "SELECT command, COUNT(*) as cnt FROM command_usage"
            " WHERE user_id = ?" + time_clause + " GROUP BY command ORDER BY cnt DESC",
            (uid, *time_params),
        )
        if not rows:
            await update.message.reply_text(f"No usage data for {uid}")
            return
        names = await _dh._fetch_discord_usernames([str(uid)])
        name = md_escape(names.get(str(uid), str(uid)))
        total = sum(r["cnt"] for r in rows)
        lines = [f"*{name}* (`{uid}`) — {total}회{period_label}\n"]
        for r in rows:
            lines.append(f"  `{r['command']:<8}` {r['cnt']}회")
        await _utils.safe_reply(update.message, "\n".join(lines))
    else:
        rows = await _cfg.fetch_rows_async(
            "SELECT user_id, command, COUNT(*) as cnt FROM command_usage"
            " WHERE 1=1" + time_clause + " GROUP BY user_id, command ORDER BY user_id, cnt DESC",
            time_params,
        )
        if not rows:
            await update.message.reply_text("No usage data yet.")
            return

        user_data: dict[str, list] = {}
        for r in rows:
            uid_str = str(r["user_id"])
            user_data.setdefault(uid_str, []).append((r["command"], r["cnt"]))

        user_totals = {uid: sum(c for _, c in cmds) for uid, cmds in user_data.items()}
        sorted_uids = sorted(user_data, key=lambda u: user_totals[u], reverse=True)

        names = await _dh._fetch_discord_usernames(sorted_uids)
        lines = [f"*📊 Command Usage Stats{period_label}*\n"]
        for uid_str in sorted_uids:
            name = md_escape(names.get(uid_str, uid_str))
            total = user_totals[uid_str]
            cmds = user_data[uid_str]
            cmd_str = "  ".join(f"`{cmd}` ×{cnt}" for cmd, cnt in cmds)
            lines.append(f"👤 *{name}* (`{uid_str}`) — 총 {total}회\n  {cmd_str}")
        await _utils.safe_reply(update.message, "\n\n".join(lines))
