"""Commands: moderation and stats (!ban, !unban, !banlist, !stats, !re0)."""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

import tg_config as _cfg
import tg_utils as _utils
import discord_helpers as _dh
from tg_utils import admin_only


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
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        # The spawned bat kills this process within ~2s; exit is handled there.

    global _restart_task
    _restart_task = asyncio.create_task(_do_restart())  # ref: survive GC until it fires


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!st [userID] — command usage stats from SQLite. No arg = summary, with arg = user detail."""
    text = (update.message.text or "").strip()
    raw_arg = text[len("!st"):].strip()

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

    conn = _cfg.open_db()
    if conn is None:
        await update.message.reply_text("DB not found.")
        return

    try:
        if arg and arg.isdigit():
            uid = int(arg)
            rows = conn.execute(
                "SELECT command, COUNT(*) as cnt FROM command_usage"
                " WHERE user_id = ?"
                + time_clause
                + " GROUP BY command ORDER BY cnt DESC",
                (uid, *time_params),
            ).fetchall()
            if not rows:
                await update.message.reply_text(f"No usage data for {uid}")
                return
            names = await _dh._fetch_discord_usernames([str(uid)])
            name = names.get(str(uid), str(uid))
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
            if not rows:
                await update.message.reply_text("No usage data yet.")
                return

            user_data: dict[str, list] = {}
            for r in rows:
                uid_str = str(r["user_id"])
                user_data.setdefault(uid_str, []).append((r["command"], r["cnt"]))

            user_totals = {
                uid: sum(c for _, c in cmds) for uid, cmds in user_data.items()
            }
            sorted_uids = sorted(user_data, key=lambda u: user_totals[u], reverse=True)

            names = await _dh._fetch_discord_usernames(sorted_uids)
            lines = [f"*📊 Command Usage Stats{period_label}*\n"]
            for uid_str in sorted_uids:
                name = names.get(uid_str, uid_str)
                total = user_totals[uid_str]
                cmds = user_data[uid_str]
                cmd_str = "  ".join(f"`{cmd}` ×{cnt}" for cmd, cnt in cmds)
                lines.append(f"👤 *{name}* (`{uid_str}`) — 총 {total}회\n  {cmd_str}")
            await _utils.safe_reply(update.message, "\n\n".join(lines))
    finally:
        conn.close()


@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!ban <userID> [minutes] — manually temp-ban a Discord user from using slash commands."""
    text = (update.message.text or "").strip()
    parts = text[len("!ban"):].strip().split()
    if not parts or not parts[0].isdigit():
        await update.message.reply_text(
            "사용법: `!ban <userID> [분]`\n예시: `!ban 123456789012345678 30`\n기본값: 5분",
            parse_mode="Markdown",
        )
        return

    uid = int(parts[0])
    minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 5
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    if not _cfg.BBS_DB_PATH.exists():
        await update.message.reply_text("DB not found.")
        return

    try:
        # Idempotent UPSERT — safe to retry on SQLITE_BUSY/locked via db_write.
        def _w(conn):
            conn.execute(
                "INSERT INTO temp_bans (user_id, expires_at) VALUES (?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET expires_at = excluded.expires_at",
                (uid, expires_at.isoformat()),
            )
        _cfg.db_write(_w)
    except Exception as e:
        await update.message.reply_text(f"❌ 밴 실패: {e}")
        return

    names = await _dh._fetch_discord_usernames([str(uid)])
    name = names.get(str(uid), str(uid))
    await update.message.reply_text(
        f"⛔ *{name}* (`{uid}`) — {minutes}분 밴 적용\n만료: `{expires_at.strftime('%Y-%m-%d %H:%M')} UTC`",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!unban <userID> — remove a temp ban immediately."""
    text = (update.message.text or "").strip()
    arg = text[len("!unban"):].strip()
    if not arg or not arg.isdigit():
        await update.message.reply_text(
            "사용법: `!unban <userID>`", parse_mode="Markdown"
        )
        return

    uid = int(arg)
    if not _cfg.BBS_DB_PATH.exists():
        await update.message.reply_text("DB not found.")
        return

    # Idempotent DELETE — safe to retry on SQLITE_BUSY/locked via db_write.
    def _w(conn):
        cur = conn.execute("DELETE FROM temp_bans WHERE user_id = ?", (uid,))
        return cur.rowcount > 0
    deleted = _cfg.db_write(_w)

    names = await _dh._fetch_discord_usernames([str(uid)])
    name = names.get(str(uid), str(uid))
    if deleted:
        await update.message.reply_text(
            f"✅ *{name}* (`{uid}`) 밴 해제됨", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"ℹ️ *{name}* (`{uid}`) 는 밴 목록에 없음", parse_mode="Markdown"
        )


@admin_only
async def cmd_banlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!banlist — show all active temp bans."""
    conn = _cfg.open_db()
    if conn is None:
        await update.message.reply_text("DB not found.")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        rows = conn.execute(
            "SELECT user_id, expires_at FROM temp_bans WHERE expires_at > ? ORDER BY expires_at",
            (now_iso,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        await update.message.reply_text("현재 밴 목록 없음")
        return

    uid_strs = [str(r["user_id"]) for r in rows]
    names = await _dh._fetch_discord_usernames(uid_strs)
    lines = ["⛔ *활성 임시밴 목록*\n"]
    for r in rows:
        uid_str = str(r["user_id"])
        name = names.get(uid_str, uid_str)
        exp = r["expires_at"][:16].replace("T", " ")
        lines.append(f"• *{name}* (`{uid_str}`) — `{exp} UTC`")
    await _utils.safe_reply(update.message, "\n".join(lines))
