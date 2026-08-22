"""Commands: subscriber management (!list, !sub, !se)."""

import json
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

import tg_config as _cfg
import tg_utils as _utils
import discord_helpers as _dh
from tg_utils import admin_only, _to_kst


@admin_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!list — subscriber list per category from SQLite."""
    conn = _cfg.open_db()
    if conn is None:
        await update.message.reply_text("DB not found.")
        return
    all_uids: set[str] = set()

    try:
        _ALLOWED_TABLES = {
            "bongnews_subscribers",
            "subscribers",
            "market_subscribers",
            "ark_subscribers",
            "morning_subscribers",
            "danawa_subscriptions",
            "pubg_subscribers",
        }
        _ALLOWED_COLS = {"user_id", "mode", "cities", "name", "ticker", "nickname"}

        def _rows(table, *cols):
            if table not in _ALLOWED_TABLES:
                raise ValueError(f"Table not allowed: {table}")
            if not all(c in _ALLOWED_COLS for c in cols):
                raise ValueError(f"Column not allowed: {set(cols) - _ALLOWED_COLS}")
            return conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()

        bongnews = _rows("bongnews_subscribers", "user_id")
        spurs = _rows("subscribers", "user_id", "mode")
        market = _rows("market_subscribers", "user_id", "mode")
        ark = _rows("ark_subscribers", "user_id")
        morning = _rows("morning_subscribers", "user_id", "cities")
        danawa = _rows("danawa_subscriptions", "user_id", "name")
        pubg = _rows("pubg_subscribers", "user_id", "nickname")
    finally:
        conn.close()

    for rows in (bongnews, spurs, market, ark, morning, pubg):
        all_uids.update(str(r["user_id"]) for r in rows)
    all_uids.update(str(r["user_id"]) for r in danawa)

    names = await _dh._fetch_discord_usernames(list(all_uids))

    def _n(uid) -> str:
        return names.get(str(uid), str(uid))

    lines = ["📋 *구독자 목록*\n"]

    def _section(label, rows, fmt):
        uids = sorted({r["user_id"] for r in rows})
        lines.append(f"*{label}* ({len(uids)}명)")
        for uid in uids:
            lines.append(fmt(uid, [r for r in rows if r["user_id"] == uid]))
        lines.append("")

    _section("봉봉뉴스", bongnews, lambda uid, _: f"  · {_n(uid)}")
    _section("토트넘/F1", spurs, lambda uid, rs: f"  · {_n(uid)} `{rs[0]['mode']}`")
    _section("시황", market, lambda uid, rs: f"  · {_n(uid)} `{rs[0]['mode']}`")
    _section("ARK", ark, lambda uid, _: f"  · {_n(uid)}")

    lines.append(f"*아침브리핑* ({len({r['user_id'] for r in morning})}명)")
    for r in morning:
        try:
            city_list = json.loads(r["cities"])
            city_str = ", ".join(city_list) if city_list else "—"
        except Exception:
            city_str = str(r["cities"])
        lines.append(f"  · {_n(r['user_id'])} — {city_str}")
    lines.append("")

    danawa_uids = sorted({r["user_id"] for r in danawa})
    lines.append(f"*다나와* ({len(danawa_uids)}명)")
    for uid in danawa_uids:
        items = ", ".join(r["name"] for r in danawa if r["user_id"] == uid)
        lines.append(f"  · {_n(uid)} — {items}")
    lines.append("")

    _section("PUBG", pubg, lambda uid, rs: f"  · {_n(uid)} `{rs[0]['nickname']}`")

    lines.append(f"전체 이용자: {len(all_uids)}명")
    await _utils.safe_reply(update.message, "\n".join(lines))


@admin_only
async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!sub [list|add <id>|del <id>] — manage bongnews subscribers."""
    text = (update.message.text or "").strip()
    arg = text[len("!sub") :].strip()
    parts = arg.split()

    if not _cfg.BBS_DB_PATH.exists():
        await update.message.reply_text("DB not found.")
        return

    subcmd = parts[0].lower() if parts else "list"

    if subcmd == "add":
        if len(parts) < 2 or not parts[1].isdigit():
            await update.message.reply_text("사용법: `!sub add <userID>`", parse_mode="Markdown")
            return
        uid = int(parts[1])
        try:
            # Idempotent INSERT OR IGNORE — safe to retry on SQLITE_BUSY/locked.
            # SELECT changes() must read on the same connection inside the fn.
            def _w(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO bongnews_subscribers (user_id) VALUES (?)", (uid,)
                )
                return conn.execute("SELECT changes()").fetchone()[0]

            affected = _cfg.db_write(_w)
        except Exception as e:
            await update.message.reply_text(f"❌ 구독 추가 실패: {e}")
            return
        names = await _dh._fetch_discord_usernames([str(uid)])
        name = names.get(str(uid), str(uid))
        if affected:
            await update.message.reply_text(
                f"✅ *{name}* (`{uid}`) 봉봉뉴스 구독 추가", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"ℹ️ *{name}* (`{uid}`) 이미 구독 중", parse_mode="Markdown"
            )

    elif subcmd == "del":
        if len(parts) < 2 or not parts[1].isdigit():
            await update.message.reply_text("사용법: `!sub del <userID>`", parse_mode="Markdown")
            return
        uid = int(parts[1])
        try:
            # Idempotent DELETE — safe to retry on SQLITE_BUSY/locked via db_write.
            def _w(conn):
                cur = conn.execute("DELETE FROM bongnews_subscribers WHERE user_id = ?", (uid,))
                return cur.rowcount > 0

            deleted = _cfg.db_write(_w)
        except Exception as e:
            await update.message.reply_text(f"❌ 구독 삭제 실패: {e}")
            return
        names = await _dh._fetch_discord_usernames([str(uid)])
        name = names.get(str(uid), str(uid))
        if deleted:
            await update.message.reply_text(
                f"✅ *{name}* (`{uid}`) 봉봉뉴스 구독 삭제", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"ℹ️ *{name}* (`{uid}`) 구독자 아님", parse_mode="Markdown"
            )

    else:
        await update.message.reply_text(
            "사용법: `!sub add <userID>` / `!sub del <userID>`\n봉봉뉴스 구독자 목록은 `!list` 참조",
            parse_mode="Markdown",
        )


@admin_only
async def cmd_sub_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!se [userID] [Nd] — subscription event log. Default: last 30d global."""
    import re as _re

    text = (update.message.text or "").strip()
    arg = text[len("!se") :].strip()
    parts = arg.split()

    days = 30
    uid = None
    for p in parts:
        if _re.match(r"^\d{1,3}d$", p):
            days = int(p[:-1])
        elif p.isdigit() and len(p) > 4:
            uid = int(p)

    conn = _cfg.open_db()
    if conn is None:
        await update.message.reply_text("DB not found.")
        return

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if uid:
            rows = conn.execute(
                "SELECT source, action, ts FROM subscription_events"
                " WHERE user_id = ? AND ts > ? ORDER BY ts DESC LIMIT 50",
                (uid, cutoff),
            ).fetchall()
            names = await _dh._fetch_discord_usernames([str(uid)])
            name = names.get(str(uid), str(uid))
            lines = [f"*{name}* 구독 이벤트 (최근 {days}일)\n"]
            for r in rows:
                ts = _to_kst(r["ts"])
                icon = "✅" if r["action"] == "subscribe" else "❌"
                lines.append(f"{icon} `{r['source']:<8}` {ts}")
        else:
            rows = conn.execute(
                "SELECT source, action, COUNT(*) as cnt FROM subscription_events"
                " WHERE ts > ? GROUP BY source, action ORDER BY source, action",
                (cutoff,),
            ).fetchall()
            lines = [f"📊 *구독 이벤트* (최근 {days}일)\n"]
            cur_src = None
            for r in rows:
                if r["source"] != cur_src:
                    cur_src = r["source"]
                    lines.append(f"*{cur_src}*")
                icon = "✅" if r["action"] == "subscribe" else "❌"
                lines.append(f"  {icon} {r['action']}: {r['cnt']}건")
            lines.append("")
            total = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM subscription_events WHERE ts > ?", (cutoff,)
            ).fetchone()[0]
            lines.append(f"관련 유저: {total}명")
    finally:
        conn.close()

    await _utils.safe_reply(update.message, "\n".join(lines))
