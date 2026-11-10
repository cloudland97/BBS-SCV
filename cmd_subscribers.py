"""Commands: subscriber management (!list, !sub, !se)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

import tg_config as _cfg
import tg_utils as _utils
import discord_helpers as _dh
from tg_utils import admin_only, _to_kst, md_escape, parse_user_id


_SUBSCRIBER_LABELS = {
    "bongnews_subscribers": "봉봉뉴스",
    "subscribers": "토트넘/F1",
    "market_subscribers": "시황",
    "ark_subscribers": "ARK",
    "morning_subscribers": "아침브리핑",
    "pubg_subscribers": "PUBG",
    "treasury_subscribers": "MSTR 공시",
    "bluearchive_subscribers": "블루 아카이브",
    "booth_subscribers": "Booth",
    "kor_nt_subscribers": "축구 국가대표",
    "tarkov_subscribers": "타르코프",
    "epic_subscribers": "에픽 무료 게임",
}
# Extra column per table that needs more than user_id; others use the generic path.
_SUBSCRIBER_EXTRA_COL = {
    "subscribers": "mode",
    "market_subscribers": "mode",
    "morning_subscribers": "cities",
    "pubg_subscribers": "nickname",
}


def _city_str(raw) -> str:
    try:
        city_list = json.loads(raw)
        return ", ".join(city_list) if city_list else "—"
    except Exception:
        return str(raw)


@admin_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!list — subscriber list per category from SQLite."""
    if not _cfg.BBS_DB_PATH.exists():
        await update.message.reply_text("DB not found.")
        return
    all_uids: set[str] = set()

    def _read(conn):
        found = _cfg.list_subscriber_tables(conn)
        known = [t for t in _SUBSCRIBER_LABELS if t in found]
        ordered = known + [t for t in found if t not in _SUBSCRIBER_LABELS]
        result = []
        for table in ordered:
            extra = _SUBSCRIBER_EXTRA_COL.get(table)
            cols = f"user_id, {extra}" if extra else "user_id"
            # Table name comes from sqlite_master, quoted as an identifier.
            ident = '"' + table.replace('"', '""') + '"'
            result.append((table, conn.execute(f"SELECT {cols} FROM {ident}").fetchall()))
        return result

    # Queries against the shared bbs.db; off the event loop so a BBS-side
    # write lock cannot stall Telegram polling.
    tables = await _cfg.db_read_async(_read)

    for _, rows in tables:
        all_uids.update(str(r["user_id"]) for r in rows)

    names = await _dh._fetch_discord_usernames(list(all_uids))

    def _n(uid) -> str:
        # Escaped here, not at each call site — a Discord global_name holding an
        # underscore would otherwise turn the whole !list message into a BadRequest.
        return md_escape(names.get(str(uid), str(uid)))

    lines = ["📋 *구독자 목록*\n"]

    for table, rows in tables:
        label = _SUBSCRIBER_LABELS.get(table, table)
        extra = _SUBSCRIBER_EXTRA_COL.get(table)
        uids = sorted({r["user_id"] for r in rows})
        lines.append(f"*{md_escape(label)}* ({len(uids)}명)")
        if extra == "cities":
            for r in rows:
                lines.append(f"  · {_n(r['user_id'])} — {md_escape(_city_str(r['cities']))}")
        else:
            for uid in uids:
                mine = [r for r in rows if r["user_id"] == uid]
                if extra == "mode":
                    lines.append(f"  · {_n(uid)} `{mine[0]['mode']}`")
                elif extra == "nickname":
                    lines.append(f"  · {_n(uid)} `{', '.join(r['nickname'] for r in mine)}`")
                else:
                    lines.append(f"  · {_n(uid)}")
        lines.append("")

    lines.append(f"전체 이용자: {len(all_uids)}명")
    await _utils.safe_reply(update.message, "\n".join(lines))


def _record_event(conn, uid: int, action: str) -> None:
    """Write the subscription_events audit row for a membership change, in the
    same transaction so the two cannot disagree. Best-effort: a missing/drifted
    table (the BBS bot's schema, not ours) must cost an audit row, not the subscription.
    """
    try:
        conn.execute(
            "INSERT INTO subscription_events (user_id, source, action) VALUES (?, 'bongnews', ?)",
            (uid, action),
        )
    except sqlite3.Error as e:
        _cfg.log.warning("subscription_events %s row for %s failed: %s", action, uid, e)


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
        uid = parse_user_id(parts[1]) if len(parts) > 1 else None
        if uid is None:
            await update.message.reply_text("사용법: `!sub add <userID>`", parse_mode="Markdown")
            return
        try:
            # Idempotent INSERT OR IGNORE — safe to retry on SQLITE_BUSY/locked.
            # SELECT changes() must read on the same connection inside the fn.
            def _w(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO bongnews_subscribers (user_id) VALUES (?)", (uid,)
                )
                affected = conn.execute("SELECT changes()").fetchone()[0]
                if affected:
                    _record_event(conn, uid, "subscribe")
                return affected

            affected = await _cfg.db_write_async(_w)
        except Exception as e:
            await update.message.reply_text(f"❌ 구독 추가 실패: {e}")
            return
        names = await _dh._fetch_discord_usernames([str(uid)])
        name = md_escape(names.get(str(uid), str(uid)))
        if affected:
            await _utils.safe_reply(
                update.message, f"✅ *{name}* (`{uid}`) 봉봉뉴스 구독 추가", parse_mode="Markdown"
            )
        else:
            await _utils.safe_reply(
                update.message, f"ℹ️ *{name}* (`{uid}`) 이미 구독 중", parse_mode="Markdown"
            )

    elif subcmd == "del":
        uid = parse_user_id(parts[1]) if len(parts) > 1 else None
        if uid is None:
            await update.message.reply_text("사용법: `!sub del <userID>`", parse_mode="Markdown")
            return
        try:
            # Idempotent DELETE — safe to retry on SQLITE_BUSY/locked via db_write.
            def _w(conn):
                cur = conn.execute("DELETE FROM bongnews_subscribers WHERE user_id = ?", (uid,))
                removed = cur.rowcount > 0
                if removed:
                    _record_event(conn, uid, "unsubscribe")
                return removed

            deleted = await _cfg.db_write_async(_w)
        except Exception as e:
            await update.message.reply_text(f"❌ 구독 삭제 실패: {e}")
            return
        names = await _dh._fetch_discord_usernames([str(uid)])
        name = md_escape(names.get(str(uid), str(uid)))
        if deleted:
            await _utils.safe_reply(
                update.message, f"✅ *{name}* (`{uid}`) 봉봉뉴스 구독 삭제", parse_mode="Markdown"
            )
        else:
            await _utils.safe_reply(
                update.message, f"ℹ️ *{name}* (`{uid}`) 구독자 아님", parse_mode="Markdown"
            )

    else:
        await update.message.reply_text(
            "사용법: `!sub add <userID>` / `!sub del <userID>`\n봉봉뉴스 구독자 목록은 `!list` 참조",
            parse_mode="Markdown",
        )


def _events_cutoff(days: int) -> str:
    """ts is "YYYY-MM-DD HH:MM:SS"; isoformat()'s "T" sorts after " ", dropping the boundary day."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


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
        else:
            candidate = parse_user_id(p)
            if candidate is not None and len(str(candidate)) > 4:
                uid = candidate

    if not _cfg.BBS_DB_PATH.exists():
        await update.message.reply_text("DB not found.")
        return

    cutoff = _events_cutoff(days)
    if uid:
        rows = await _cfg.fetch_rows_async(
            "SELECT source, action, ts FROM subscription_events"
            " WHERE user_id = ? AND ts > ? ORDER BY ts DESC LIMIT 50",
            (uid, cutoff),
        )
        names = await _dh._fetch_discord_usernames([str(uid)])
        name = md_escape(names.get(str(uid), str(uid)))
        lines = [f"*{name}* 구독 이벤트 (최근 {days}일)\n"]
        for r in rows:
            ts = _to_kst(r["ts"])
            icon = "✅" if r["action"] == "subscribe" else "❌"
            lines.append(f"{icon} `{r['source']:<8}` {ts}")
    else:
        rows = await _cfg.fetch_rows_async(
            "SELECT source, action, COUNT(*) as cnt FROM subscription_events"
            " WHERE ts > ? GROUP BY source, action ORDER BY source, action",
            (cutoff,),
        )
        lines = [f"📊 *구독 이벤트* (최근 {days}일)\n"]
        cur_src = None
        for r in rows:
            if r["source"] != cur_src:
                cur_src = r["source"]
                lines.append(f"*{cur_src}*")
            icon = "✅" if r["action"] == "subscribe" else "❌"
            lines.append(f"  {icon} {r['action']}: {r['cnt']}건")
        lines.append("")
        total = (
            await _cfg.fetch_rows_async(
                "SELECT COUNT(DISTINCT user_id) FROM subscription_events WHERE ts > ?", (cutoff,)
            )
        )[0][0]
        lines.append(f"관련 유저: {total}명")

    await _utils.safe_reply(update.message, "\n".join(lines))
