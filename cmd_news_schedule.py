"""Scheduled bongnews — parse time from !news, persist to DB, scheduler helpers."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from tg_config import db_write, get_bbs_db
from tg_utils import admin_only, ensure_utc

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


def _ensure_table():
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

    return db_write(_w)


def mark_status(news_id: int, status: str):
    # Idempotent status UPDATE — safe to retry on SQLITE_BUSY/locked via db_write.
    def _w(conn):
        conn.execute("UPDATE scheduled_news SET status = ? WHERE id = ?", (status, news_id))

    db_write(_w)


@admin_only
async def cmd_newslist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!newslist — show pending scheduled news."""
    rows = get_pending()
    if not rows:
        await update.message.reply_text("예약된 뉴스 없음.")
        return
    lines = [f"📅 *예약된 뉴스 {len(rows)}건*\n"]
    for r in rows:
        dt_utc = ensure_utc(datetime.fromisoformat(r["scheduled_at"]))
        dt_kst = dt_utc.astimezone(KST)
        preview = r["content"][:40] + ("..." if len(r["content"]) > 40 else "")
        lines.append(f"`[{r['id']}]` {dt_kst.strftime('%m/%d %H:%M')} KST\n  {preview}")
    lines.append("\n취소: `!newscc <ID>`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def cmd_newscc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!newscc <id> — cancel a scheduled news item."""
    text = (update.message.text or "").strip()
    arg = text[len("!newscc") :].strip()
    if not arg or not arg.isdigit():
        await update.message.reply_text(
            "Usage: `!newscc <ID>`\n목록 확인: `!newslist`", parse_mode="Markdown"
        )
        return
    news_id = int(arg)
    conn = get_bbs_db()
    try:
        row = conn.execute(
            "SELECT id, content, scheduled_at FROM scheduled_news WHERE id = ? AND status = 'pending'",
            (news_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        await update.message.reply_text(f"ID {news_id} 예약 없음 (이미 발송됐거나 취소됨).")
        return
    if not set_status_if_pending(news_id, "cancelled"):
        # The scheduler claimed it between the SELECT above and this UPDATE.
        await update.message.reply_text(
            f"⚠️ ID {news_id} 는 방금 발송이 시작돼 취소하지 못했습니다."
        )
        return

    dt_utc = ensure_utc(datetime.fromisoformat(row["scheduled_at"]))
    dt_kst = dt_utc.astimezone(KST)
    preview = row["content"][:40]
    await update.message.reply_text(
        f"✅ 예약 취소됨 `[{news_id}]`\n📅 {dt_kst.strftime('%m/%d %H:%M')} KST\n📝 {preview}",
        parse_mode="Markdown",
    )
