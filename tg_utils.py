"""Telegram bridge utilities — message splitting, HUD, formatting helpers."""

import re
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional

from telegram.error import BadRequest

from tg_config import ALLOWED_USER_ID, log

# Telegram hard-caps a message at 4096 chars; 4000 leaves headroom for markdown.
TG_MSG_LIMIT = 4000


def u16_len(s: str) -> int:
    """Length in UTF-16 code units, the unit Telegram and Discord actually cap.

    Python counts code points, so 1500 non-BMP emoji measure as 1500 locally but 3000 units to both APIs.
    """
    return len(s.encode("utf-16-le")) // 2


def _split_at_units(s: str, limit: int) -> tuple[str, str]:
    """Cut s so the head is at most `limit` UTF-16 units, never mid-code-point —
    a split surrogate pair is rejected by the API and mangles the emoji."""
    used = 0
    for i, ch in enumerate(s):
        ch_len = 2 if ord(ch) > 0xFFFF else 1
        if used + ch_len > limit:
            # A limit narrower than one code point would otherwise loop forever
            # emitting empty heads; emit the whole code point instead.
            return (s[:1], s[1:]) if i == 0 else (s[:i], s[i:])
        used += ch_len
    return s, ""


def split_message(text, limit=TG_MSG_LIMIT):
    """Split text into chunks of at most `limit` UTF-16 units, preferring line boundaries.

    Long messages (!list/!st/!hp) exceed Telegram's cap and would otherwise fail silently.
    """
    if u16_len(text) <= limit:
        return [text]
    chunks, current, current_len = [], [], 0
    for line in text.splitlines(keepends=True):
        line_len = u16_len(line)
        if current_len + line_len > limit and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        while line_len > limit:
            head, line = _split_at_units(line, limit)
            chunks.append(head)
            line_len = u16_len(line)
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("".join(current))
    return chunks


# Telegram Markdown (v1) specials, backslash included (the escape char
# itself). Arbitrary interpolated content (news text, a Discord username) can
# otherwise turn the message into a BadRequest.
_MD_SPECIALS_RE = re.compile(r"([_*`\[\\])")


def md_escape(text: str) -> str:
    """Backslash-escape Telegram Markdown v1 specials in arbitrary text."""
    return _MD_SPECIALS_RE.sub(r"\\\1", text or "")


# Discord renders mentions as <@id> (and legacy <@!id>); accepting that form
# lets an operator paste straight from Discord instead of digging out the id.
_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def parse_int_arg(raw: Optional[str]) -> Optional[int]:
    """int() an argument only when it is ASCII digits, else None.

    str.isdigit() is True for Unicode digit forms that int() then rejects (e.g. U+00B2 SUPERSCRIPT TWO).
    """
    raw = (raw or "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() else None


def parse_user_id(raw: Optional[str]) -> Optional[int]:
    """Parse a Discord user id from a bare number or a <@id> / <@!id> mention."""
    raw = (raw or "").strip()
    m = _MENTION_RE.match(raw)
    if m:
        raw = m.group(1)
    return parse_int_arg(raw)


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
        # effective_user is None for channel posts; check before dereferencing to
        # avoid an AttributeError before the permission check runs.
        user = update.effective_user
        if user is None or user.id != ALLOWED_USER_ID:
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
        except Exception as e:
            log.error(f"safe_reply error: {e}")


async def safe_send(bot, chat_id, text, parse_mode="Markdown"):
    """Bot-level counterpart to safe_reply, for sends with no Update to reply to.

    Boot notifications interpolate stored news content into Markdown, so a broken row could raise BadRequest and kill startup without this guard.
    """
    for chunk in split_message(text):
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
        except BadRequest:
            try:
                await bot.send_message(chat_id=chat_id, text=chunk)
            except Exception as e:
                log.error(f"safe_send error: {e}")
        except Exception as e:
            log.error(f"safe_send error: {e}")
