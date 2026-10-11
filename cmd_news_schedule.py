"""Scheduled bongnews — parse time from !news, persist to DB, scheduler helpers."""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

import tg_config as _cfg
from tg_config import db_write, get_bbs_db
from tg_utils import admin_only, ensure_utc, md_escape, parse_int_arg, safe_reply

KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc

TIME_FORMAT_HELP = (
    "시간 지정 형식:\n"
    "• `!news 08:30 내용` — 오늘 08:30 KST (지났으면 내일)\n"
    "• `!news 내일 08:30 내용` — 내일\n"
    "• `!news 4/25 08:30 내용` — 날짜 지정\n\n"
    "즉시 발송: `!news 내용` (시간 없이)\n"
    "예약 목록: `!newslist` | 취소: `!newscc <ID>`"
)

_NO_TIME = "NO_TIME"


# Databases whose scheduled_news this process has already migrated. Keyed by
# path, not a plain flag, so tests that swap in a temp DB still run it.
_ensured_paths: set[str] = set()


def reset_ensure_cache() -> None:
    """Forget which databases have been migrated (test helper)."""
    _ensured_paths.clear()


def _ensure_table():
    key = str(_cfg.BBS_DB_PATH)
    if key in _ensured_paths:
        return

    # Idempotent CREATE TABLE IF NOT EXISTS — safe to retry on SQLITE_BUSY/locked.
    def _w(conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_news (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content     TEXT    NOT NULL,
                scheduled_at TEXT   NOT NULL,
                created_at  TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending'
            )
        """)
        # Guarded migration for the claim-before-send column. SQLite has no
        # "ADD COLUMN IF NOT EXISTS", so probe PRAGMA table_info first and ADD
        # only when missing. Idempotent and non-destructive — this is the shared
        # bbs.db, so running _ensure_table twice must never error or drop data.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(scheduled_news)").fetchall()}
        if "claimed_at" not in cols:
            conn.execute("ALTER TABLE scheduled_news ADD COLUMN claimed_at TEXT")

    db_write(_w)
    _ensured_paths.add(key)


def parse_schedule_time(raw: str) -> tuple[datetime, str]:
    """Extract scheduled UTC datetime and content from raw text.

    Returns (scheduled_at_utc, content).
    Raises ValueError(_NO_TIME) if no time prefix found (→ caller does immediate send).
    Raises ValueError(message) for invalid time values.
    """
    raw = raw.strip()
    now_kst = datetime.now(KST)

    # 내일 HH:MM content
    m = re.match(r"^내일\s+(\d{1,2}):(\d{2})\s*(.*)", raw, re.DOTALL)
    if m:
        h, mi, content = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        _validate_hm(h, mi)
        target = (now_kst + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return target.astimezone(UTC), content

    # HH:MM 내일 content
    m = re.match(r"^(\d{1,2}):(\d{2})\s+내일\s*(.*)", raw, re.DOTALL)
    if m:
        h, mi, content = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        _validate_hm(h, mi)
        target = (now_kst + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return target.astimezone(UTC), content

    # M/D HH:MM content
    m = re.match(r"^(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*(.*)", raw, re.DOTALL)
    if m:
        mo, d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        content = m.group(5).strip()
        _validate_hm(h, mi)
        year = now_kst.year
        try:
            target = datetime(year, mo, d, h, mi, 0, tzinfo=KST)
        except ValueError:
            raise ValueError(f"날짜가 잘못됐어요: {mo}/{d}")
        if target < now_kst:
            try:
                target = datetime(year + 1, mo, d, h, mi, 0, tzinfo=KST)
            except ValueError:
                raise ValueError(f"날짜가 잘못됐어요: {mo}/{d}")
        return target.astimezone(UTC), content

    # HH:MM content  (오늘, 지났으면 내일)
    m = re.match(r"^(\d{1,2}):(\d{2})\s+(.*)", raw, re.DOTALL)
    if m:
        h, mi, content = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        _validate_hm(h, mi)
        target = now_kst.replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= now_kst:
            target += timedelta(days=1)
        return target.astimezone(UTC), content

    raise ValueError(_NO_TIME)


def _validate_hm(h: int, mi: int):
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"시간이 잘못됐어요: {h:02d}:{mi:02d}")


def save_scheduled(content: str, scheduled_at_utc: datetime) -> int:
    # Intentionally single-attempt (NOT routed through db_write): a bare INSERT
    # returning lastrowid is non-idempotent — a retry would risk a duplicate row
    # (double-scheduled news). On lock the OperationalError must propagate.
    _ensure_table()
    conn = get_bbs_db()
    try:
        cur = conn.execute(
            "INSERT INTO scheduled_news (content, scheduled_at, created_at, status) VALUES (?, ?, ?, 'pending')",
            (content, scheduled_at_utc.isoformat(), datetime.now(UTC).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_pending() -> list[dict]:
    _ensure_table()
    conn = get_bbs_db()
    try:
        rows = conn.execute(
            "SELECT id, content, scheduled_at FROM scheduled_news "
            "WHERE status = 'pending' ORDER BY scheduled_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# Retry budget for rows that could never be attempted (no token/no subscribers).
# Without a cap, "stays pending" live-locks the 30s scheduler forever. Three
# attempts, then terminal. Kept in memory (not a DB fact) so a restart retries afresh.
MAX_UNATTEMPTED_ATTEMPTS = 3
_send_attempts: dict[int, int] = {}


def record_unattempted(news_id: int) -> int:
    """Count one cycle in which the broadcast could not be attempted."""
    _send_attempts[news_id] = _send_attempts.get(news_id, 0) + 1
    return _send_attempts[news_id]


def forget_attempts(news_id: int) -> None:
    """Drop a row's retry budget once it reaches a state it cannot leave."""
    _send_attempts.pop(news_id, None)


def reset_send_attempts() -> None:
    """Clear every retry budget (test helper)."""
    _send_attempts.clear()


# How far back !newslist looks for failures — older is history, not a to-do,
# and an unbounded list would crowd the pending rows off the message.
_FAILED_WINDOW_DAYS = 7


def get_failed(limit: int = 10, days: int = _FAILED_WINDOW_DAYS) -> list[dict]:
    """Recent rows whose broadcast reached nobody, newest first.

    'failed' is terminal (get_due only looks at 'pending'), so without this list a zero-subscriber send left no trace.
    """
    _ensure_table()
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn = get_bbs_db()
    try:
        rows = conn.execute(
            "SELECT id, content, scheduled_at FROM scheduled_news "
            "WHERE status = 'failed' AND scheduled_at >= ? "
            "ORDER BY scheduled_at DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_due(now_utc: datetime) -> list[dict]:
    _ensure_table()
    conn = get_bbs_db()
    try:
        rows = conn.execute(
            "SELECT id, content, scheduled_at FROM scheduled_news "
            "WHERE status = 'pending' AND scheduled_at <= ?",
            (now_utc.isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def claim_for_send(news_id: int) -> bool:
    """Atomically claim a pending row for sending. Returns True iff this call won
    the claim (rowcount == 1).

    The `WHERE status = 'pending'` guard means only ONE caller can flip a row to
    'sending'; a concurrent or next-30s-cycle attempt sees rowcount 0 and skips.
    Claiming BEFORE broadcast keeps get_due (pending-only) from re-selecting the
    row during the send window, so a slow/failed mark_status can never cause a
    duplicate broadcast. Routed through db_write: on lock the whole UPDATE retries
    fresh, and it stays idempotent-safe because the row is still 'pending' until
    one attempt commits.
    """
    _ensure_table()
    claimed_at = datetime.now(UTC).isoformat()

    def _w(conn):
        cur = conn.execute(
            "UPDATE scheduled_news SET status = 'sending', claimed_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (claimed_at, news_id),
        )
        return cur.rowcount == 1

    return db_write(_w)


def get_stale_sending(older_than_utc: datetime) -> list[dict]:
    """Rows stuck in 'sending' whose claim is older than the cutoff — orphaned by
    a crash between a successful broadcast and mark_status('sent').

    Detect-only: callers must NOT auto-resend these, because a crash AFTER the
    broadcast actually went out would then double-send. The human decides.
    """
    _ensure_table()
    conn = get_bbs_db()
    try:
        rows = conn.execute(
            "SELECT id, content, scheduled_at, claimed_at FROM scheduled_news "
            "WHERE status = 'sending' AND claimed_at IS NOT NULL AND claimed_at <= ? "
            "ORDER BY claimed_at",
            (older_than_utc.isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# claimed_at is already present in the first tracked commit (1.0, 2026-08-21); every
# 'sending' write since then sets it, so only rows created before that can lack it.
_CLAIM_COLUMN_SINCE = "2026-08-21"


def get_unclaimed_sending() -> list[dict]:
    """'sending' rows with no claimed_at, created after the claim column existed (invariant breach)."""
    _ensure_table()
    conn = get_bbs_db()
    try:
        rows = conn.execute(
            "SELECT id FROM scheduled_news "
            "WHERE status = 'sending' AND claimed_at IS NULL AND created_at >= ? ORDER BY id",
            (_CLAIM_COLUMN_SINCE,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_status_if_pending(news_id: int, status: str) -> bool:
    """Flip a still-pending row to *status*. Returns True iff this call won.

    Same guard as claim_for_send: without `WHERE status = 'pending'`, a cancel
    (or boot-time expiry) issued while the scheduler is mid-broadcast would
    stomp the row's real state, so !newscc could report "cancelled" for news
    that actually went out.
    """

    def _w(conn):
        cur = conn.execute(
            "UPDATE scheduled_news SET status = ? WHERE id = ? AND status = 'pending'",
            (status, news_id),
        )
        return cur.rowcount == 1

    changed = db_write(_w)
    if changed:
        forget_attempts(news_id)
    return changed


def cancel_cancellable(news_id: int) -> bool:
    """Cancel a row that has not gone out: 'pending' or 'failed'.

    'sending' is excluded (may be mid-broadcast) — force_release is the separate escape hatch for that case.
    """

    def _w(conn):
        cur = conn.execute(
            "UPDATE scheduled_news SET status = 'cancelled' "
            "WHERE id = ? AND status IN ('pending', 'failed')",
            (news_id,),
        )
        return cur.rowcount == 1

    _ensure_table()
    cancelled = db_write(_w)
    if cancelled:
        forget_attempts(news_id)
    return cancelled


def force_release(news_id: int) -> bool:
    """Flip a row stuck in 'sending' to 'cancelled'. True iff a row moved.

    A crash between claim_for_send and mark_status strands a row here forever;
    kept separate from plain cancel, which must never touch a mid-broadcast row.
    """

    def _w(conn):
        cur = conn.execute(
            "UPDATE scheduled_news SET status = 'cancelled' WHERE id = ? AND status = 'sending'",
            (news_id,),
        )
        return cur.rowcount == 1

    _ensure_table()
    released = db_write(_w)
    if released:
        forget_attempts(news_id)
    return released


def mark_status(news_id: int, status: str):
    # Idempotent status UPDATE — safe to retry on SQLITE_BUSY/locked via db_write.
    def _w(conn):
        conn.execute("UPDATE scheduled_news SET status = ? WHERE id = ?", (status, news_id))

    db_write(_w)


# Async wrappers: the sync functions above can sleep seconds retrying
# SQLITE_BUSY, which would freeze the polling loop if awaited directly.


async def a_get_pending() -> list[dict]:
    return await asyncio.to_thread(get_pending)


async def a_get_due(now_utc: datetime) -> list[dict]:
    return await asyncio.to_thread(get_due, now_utc)


async def a_get_failed(limit: int = 10) -> list[dict]:
    return await asyncio.to_thread(get_failed, limit)


async def a_cancel_cancellable(news_id: int) -> bool:
    return await asyncio.to_thread(cancel_cancellable, news_id)


async def a_save_scheduled(content: str, scheduled_at_utc: datetime) -> int:
    return await asyncio.to_thread(save_scheduled, content, scheduled_at_utc)


async def a_claim_for_send(news_id: int) -> bool:
    return await asyncio.to_thread(claim_for_send, news_id)


async def a_mark_status(news_id: int, status: str) -> None:
    return await asyncio.to_thread(mark_status, news_id, status)


async def a_set_status_if_pending(news_id: int, status: str) -> bool:
    return await asyncio.to_thread(set_status_if_pending, news_id, status)


async def a_get_stale_sending(older_than_utc: datetime) -> list[dict]:
    return await asyncio.to_thread(get_stale_sending, older_than_utc)


async def a_get_unclaimed_sending() -> list[dict]:
    return await asyncio.to_thread(get_unclaimed_sending)


async def a_force_release(news_id: int) -> bool:
    return await asyncio.to_thread(force_release, news_id)


@admin_only
async def cmd_newslist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!newslist — show pending scheduled news, plus recent failures.

    'failed' is terminal, so this is the only place an operator learns a scheduled send reached nobody.
    """
    rows = await a_get_pending()
    failed = await a_get_failed()
    if not rows and not failed:
        await update.message.reply_text("예약된 뉴스 없음.")
        return
    lines = [f"📅 *예약된 뉴스 {len(rows)}건*\n"]
    for r in rows:
        dt_utc = ensure_utc(datetime.fromisoformat(r["scheduled_at"]))
        dt_kst = dt_utc.astimezone(KST)
        preview = md_escape(r["content"][:40]) + ("..." if len(r["content"]) > 40 else "")
        lines.append(f"`[{r['id']}]` {dt_kst.strftime('%m/%d %H:%M')} KST\n  {preview}")
    if failed:
        lines.append(f"\n*FAILED - reached nobody ({len(failed)})*")
        for r in failed:
            dt_kst = ensure_utc(datetime.fromisoformat(r["scheduled_at"])).astimezone(KST)
            preview = md_escape(r["content"][:40]) + ("..." if len(r["content"]) > 40 else "")
            lines.append(f"`[{r['id']}]` {dt_kst.strftime('%m/%d %H:%M')} KST\n  {preview}")
    lines.append("\n취소: `!newscc <ID>`")
    await safe_reply(update.message, "\n".join(lines))


@admin_only
async def cmd_newscc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!newscc <id> [force] — cancel a scheduled news item.

    Plain form clears 'pending'/'failed'; `force` is the escape hatch for a row stranded in 'sending' by a crash mid-broadcast.
    """
    text = (update.message.text or "").strip()
    arg = text[len("!newscc") :].strip()
    parts = arg.split()
    news_id = parse_int_arg(parts[0]) if parts else None
    if news_id is None:
        await safe_reply(
            update.message, "Usage: `!newscc <ID>`\n목록 확인: `!newslist`", parse_mode="Markdown"
        )
        return

    if len(parts) > 1 and parts[1].lower() == "force":
        if await a_force_release(news_id):
            await safe_reply(
                update.message,
                f"🔓 `[{news_id}]` 강제 해제: 'sending' → 'cancelled'. "
                "재예약 전에 실제 발송 여부를 먼저 확인하세요.",
            )
        else:
            await safe_reply(
                update.message, f"ID {news_id}: 'sending' 상태로 멈춘 예약이 없습니다."
            )
        return

    # Off the event loop: opening the shared bbs.db can block on a BBS-side write.
    row = await _cfg.db_read_async(
        lambda conn: conn.execute(
            "SELECT id, content, scheduled_at FROM scheduled_news "
            "WHERE id = ? AND status IN ('pending', 'failed')",
            (news_id,),
        ).fetchone()
    )
    if not row:
        await update.message.reply_text(f"ID {news_id} 예약 없음 (이미 발송됐거나 취소됨).")
        return
    if not await a_cancel_cancellable(news_id):
        # The scheduler claimed it between the SELECT above and this UPDATE.
        await update.message.reply_text(
            f"⚠️ ID {news_id} 는 방금 발송이 시작돼 취소하지 못했습니다."
        )
        return

    dt_utc = ensure_utc(datetime.fromisoformat(row["scheduled_at"]))
    dt_kst = dt_utc.astimezone(KST)
    preview = md_escape(row["content"][:40])
    await safe_reply(
        update.message,
        f"✅ 예약 취소됨 `[{news_id}]`\n📅 {dt_kst.strftime('%m/%d %H:%M')} KST\n📝 {preview}",
        parse_mode="Markdown",
    )
