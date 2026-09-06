"""Commands: bridge restart and usage stats (!re0, !st)."""

import asyncio
import re
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
        import subprocess

        bat = Path(__file__).parent / "start_bot.bat"
        subprocess.Popen(
            ["cmd", "/c", "start", "", "/min", str(bat)],
            cwd=str(bat.parent),
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        # The spawned bat kills this process within ~2s; exit is handled there.

    global _restart_task
    _restart_task = asyncio.create_task(_do_restart())  # ref: survive GC until it fires


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

    # Opened on the event loop. Bounded exposure: these are plain SELECTs
    # with no retry sleep, so the worst stall is get_bbs_db's 4s busy
    # timeout. Every write, and the multi-query !list/!hp/!newscc paths, go
    # through db_write_async/db_read_async instead.
    conn = _cfg.open_db()
    if conn is None:
        await update.message.reply_text("DB not found.")
        return

    uid = parse_user_id(arg)
    try:
        if uid is not None:
            rows = conn.execute(
                "SELECT command, COUNT(*) as cnt FROM command_usage"
                " WHERE user_id = ?" + time_clause + " GROUP BY command ORDER BY cnt DESC",
                (uid, *time_params),
            ).fetchall()
            # Release the shared DB before any HTTP await: this handler used to
            # keep the connection open across a 15s Discord username lookup.
            conn.close()
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
            rows = conn.execute(
                "SELECT user_id, command, COUNT(*) as cnt FROM command_usage"
                " WHERE 1=1"
                + time_clause
                + " GROUP BY user_id, command ORDER BY user_id, cnt DESC",
                time_params,
            ).fetchall()
            conn.close()
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
    finally:
        conn.close()
