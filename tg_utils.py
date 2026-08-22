"""Telegram bridge utilities — message splitting, HUD, formatting helpers."""

from datetime import datetime, timedelta, timezone
from functools import wraps

from telegram.error import BadRequest

from tg_config import ALLOWED_USER_ID, log

# Telegram hard-caps a message at 4096 chars; 4000 leaves headroom for markdown.
TG_MSG_LIMIT = 4000


def split_message(text, limit=TG_MSG_LIMIT):
    """Split text into <=limit-char chunks, preferring line boundaries.

    Long messages (!list/!st/!hp) exceed Telegram's 4096-char cap and would
    otherwise fail silently; this breaks them into sendable pieces without
    losing any content.
    """
    if len(text) <= limit:
        return [text]
    chunks, current, current_len = [], [], 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > limit and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def ensure_utc(dt: datetime) -> datetime:
    """Normalize a naive datetime to UTC-aware; leave aware datetimes unchanged.

    Stored timestamps are UTC but may be parsed tz-naive; this guards
    `now_utc - dt` style arithmetic against a naive/aware mismatch.
    """
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_kst(iso: str) -> str:
    """UTC ISO string from DB → KST MM-DD HH:MM."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(hours=9)).strftime("%m-%d %H:%M")


def admin_only(func):
    """Decorator: reject non-admin users silently."""

    @wraps(func)
    async def wrapper(update, context):
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        return await func(update, context)

    return wrapper


async def safe_reply(message, text, parse_mode="Markdown"):
    """Reply, splitting into chunks so long output isn't dropped at Telegram's cap.

    Each chunk is sent with parse_mode, falling back to plain text on BadRequest
    (e.g. broken markdown), preserving the original single-message behavior.
    """
    for chunk in split_message(text):
        try:
            await message.reply_text(chunk, parse_mode=parse_mode)
        except BadRequest:
            try:
                await message.reply_text(chunk)
            except Exception as e:
                log.error(f"safe_reply error: {e}")
