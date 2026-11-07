"""Discord API helpers — token, usernames, DM sending, bongnews broadcast."""

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
from telegram import Update

from tg_config import (
    DISCORD_API,
    DISCORD_DATA_PATH,
    DISCORD_ENV_PATH,
    KST,
    log,
    open_db,
)
from tg_utils import safe_reply, split_message, u16_len

# Cap Discord API calls so a hung request can't stall a broadcast for minutes
# (aiohttp's default total timeout is 300s).
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Seam for the rate-limit backoff. Tests replace this to observe the wait
# without swapping asyncio.sleep out from under the whole interpreter.
_sleep = asyncio.sleep


def _get_discord_token() -> Optional[str]:
    try:
        for line in DISCORD_ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("DISCORD_TOKEN="):
                return line.split("=", 1)[1].strip()
    except Exception as e:
        log.warning(f"Discord token unreadable at {DISCORD_ENV_PATH}: {e}")
    return None


async def _get_discord_token_async() -> Optional[str]:
    """Read the Discord token off the event loop — a blocking read stalls Telegram polling if the file lives on a stalled network/encrypted volume."""
    return await asyncio.to_thread(_get_discord_token)


_username_cache: dict[str, tuple[str, float]] = {}  # {uid: (name, timestamp)}
_USERNAME_CACHE_TTL = 300  # 5 minutes
_USERNAME_CACHE_MAX = 500  # evict oldest when exceeded


async def _fetch_discord_usernames(uids: list[str]) -> dict[str, str]:
    """Fetch Discord usernames by user ID. Returns {uid: username} dict, ID as fallback.
    Results are cached for 5 minutes to avoid repeated API calls."""
    token = await _get_discord_token_async()
    if not token:
        return {}
    now = time.time()

    # Evict oldest entries by timestamp when over the cap (regardless of expiry),
    # so the cache cannot grow unbounded with live entries.
    if len(_username_cache) > _USERNAME_CACHE_MAX:
        ordered = sorted(_username_cache.items(), key=lambda kv: kv[1][1])
        for k, _ in ordered[: len(_username_cache) - _USERNAME_CACHE_MAX]:
            del _username_cache[k]

    result = {}
    to_fetch = []
    for uid in uids:
        cached = _username_cache.get(uid)
        if cached and now - cached[1] < _USERNAME_CACHE_TTL:
            result[uid] = cached[0]
        else:
            to_fetch.append(uid)

    if not to_fetch:
        return result

    headers = {"Authorization": f"Bot {token}"}

    async def _fetch_one(session, uid):
        try:
            async with session.get(f"{DISCORD_API}/users/{uid}", headers=headers) as r:
                if r.status == 200:
                    data = await r.json()
                    return uid, data.get("global_name") or data.get("username") or uid
        except Exception as e:
            # The caller falls back to showing the raw id, so a failure here is
            # cosmetic - but silence made auth/rate-limit errors indistinguishable
            # from "user not found" in !st / !list / !ud / !se output.
            log.debug(f"username fetch failed for {uid}: {e}")
        return uid, uid

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        pairs = await asyncio.gather(*[_fetch_one(session, uid) for uid in to_fetch])
    for uid, name in pairs:
        _username_cache[uid] = (name, now)
        result[uid] = name
    return result


def _get_bongnews_subscribers():
    """Return the subscriber list, or None when bbs.db could not be read.

    None and [] must stay apart: a lock or a schema change otherwise reads to the
    operator as "nobody is subscribed".
    """
    try:
        conn = open_db()  # None when bbs.db is absent — never create a stray file
        if conn is None:
            log.warning("bongnews subscribers unavailable: bbs.db not found")
            return None
        try:
            rows = conn.execute("SELECT user_id FROM bongnews_subscribers").fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"bongnews subscribers fetch failed: {type(e).__name__} {e}")
        return None


def _save_latest(content: str) -> None:
    """Write the newest bongnews body for the BBS feed embed to pick up (best-effort — a write failure is logged, not fatal).

    An image-only broadcast carries no text; writing it would blank the feed
    headline, so the previous one is kept instead (2026-09-12).
    """
    body = content.strip()
    if not body:
        log.info("bongnews latest not updated: broadcast carried no text")
        return
    try:
        # temp + os.replace: the BBS feed must never read a half-written file.
        latest_path = DISCORD_DATA_PATH / "bongnews_latest.txt"
        tmp_path = latest_path.with_name(latest_path.name + ".tmp")
        tmp_path.write_text(body, encoding="utf-8")
        # Windows refuses the rename while BBS has the file open; that read is brief.
        for attempt in range(3):
            try:
                os.replace(tmp_path, latest_path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.2)
    except Exception as e:
        log.warning(f"Failed to save bongnews latest: {e}")


async def _save_latest_async(content: str) -> None:
    """Write the latest-bongnews file off the event loop."""
    await asyncio.to_thread(_save_latest, content)


async def _get_bongnews_subscribers_async():
    """Read the subscriber list off the event loop — opening bbs.db can block up to the 4s busy timeout while the Discord bot holds a write lock."""
    return await asyncio.to_thread(_get_bongnews_subscribers)


# Discord rejects a message whose content exceeds 2000 characters with a 400,
# which previously failed the entire broadcast for any long news item.
_DISCORD_CONTENT_LIMIT = 2000

# Four in flight keeps a large subscriber list well inside Discord's per-route
# budget while staying far faster than serial sending.
_DM_CONCURRENCY = 4

# One retry: a 429 usually clears within its Retry-After, and a second failure
# means the whole route is throttled, where more attempts only make it worse.
_MAX_ATTEMPTS = 2
_MAX_RETRY_WAIT = 10.0

# The bridge broadcasts operator-authored text verbatim. Without this, an @here
# or a role mention inside a news item would ping every recipient's server.
_ALLOWED_MENTIONS = {"parse": []}

_NEWS_COLOR = 0xE67E22
# Discord caps an embed description at 4096; longer news falls back to chunked plain text.
_NEWS_EMBED_MAX = 4000
_NEWS_PREVIEW_MAX = 80
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _plain_line(line: str) -> str:
    """One line with Discord markdown removed, for the push preview (lock screens show raw markup)."""
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"^\s*(?:-#|#+|>+)\s*", "", line)
    line = re.sub(r"\|\|.*?\|\|", "", line)
    line = re.sub(r"<a?:(\w+):\d+>", r":\1:", line)
    line = re.sub(r"(\*\*|__|~~|\*|_|`)(.+?)\1", r"\2", line)
    if re.fullmatch(r"[-*_=\s]*", line):
        return ""
    return re.sub(r"\s+", " ", line).strip()


def _news_message(content: str, filename: Optional[str] = None, now: Optional[datetime] = None):
    """Return (text, embed, upload_name) for one bongnews DM; embed is None on the long plain-text path."""
    body = content.strip()
    if u16_len(body) > _NEWS_EMBED_MAX:
        return f"**bongnews**\n\n{content}", None, filename
    d = (now or datetime.now(KST)).astimezone(KST)
    embed = {"title": f"📰 {d.month}.{d.day} 봉봉뉴스", "color": _NEWS_COLOR}
    if body:
        embed["description"] = body
    text = "📰 봉봉뉴스"
    first = next(filter(None, map(_plain_line, body.split("\n"))), "")
    if first:
        if len(first) > _NEWS_PREVIEW_MAX:
            first = first[: _NEWS_PREVIEW_MAX - 1] + "…"
        text += f" · {first}"
    upload_name = filename
    ext = Path(filename).suffix.lower() if filename else ""
    if ext in _IMAGE_EXTS:
        # attachment:// must match the stored name exactly, and Discord rewrites spaces/non-ASCII.
        upload_name = f"bongnews{ext}"
        embed["image"] = {"url": f"attachment://{upload_name}"}
    return text, embed, upload_name


async def _retry_after(response) -> float:
    """Seconds to wait after a 429, from the Retry-After header or JSON body."""
    try:
        header = response.headers.get("Retry-After")
        if header:
            return min(float(header), _MAX_RETRY_WAIT)
    except Exception:
        pass
    try:
        value = (await response.json()).get("retry_after")
        if value is not None:
            return min(float(value), _MAX_RETRY_WAIT)
    except Exception:
        pass
    return 1.0


async def _post_message(session, url, headers, *, json_body=None, data=None) -> int:
    """POST a Discord message, retrying once on 429. Returns the status code.

    Returning the status (not a bool) lets callers distinguish 429/403/500
    instead of collapsing them into one meaning.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with session.post(url, json=json_body, data=data, headers=headers) as r:
                status = r.status
                if status == 429 and attempt < _MAX_ATTEMPTS:
                    delay = await _retry_after(r)
                    log.warning("Discord 429 on %s - retrying in %.1fs", url, delay)
                    await _sleep(delay)
                    continue
                if status not in (200, 201):
                    log.warning("Discord POST %s -> HTTP %s", url, status)
                return status
        except Exception as e:
            log.warning("Discord POST %s failed: %s", url, e)
            return 0
    return 0


async def _open_dm_channel(session, headers, user_id) -> Optional[str]:
    """Open (or reuse) the DM channel for a user. None means the DM is not
    reachable - blocked bot, left server, or a throttle that outlived a retry."""
    url = f"{DISCORD_API}/users/@me/channels"
    body = {"recipient_id": str(user_id)}
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with session.post(url, json=body, headers=headers) as r:
                if r.status == 200:
                    return (await r.json())["id"]
                if r.status == 429 and attempt < _MAX_ATTEMPTS:
                    delay = await _retry_after(r)
                    log.warning("Discord 429 opening DM for %s - retrying in %.1fs", user_id, delay)
                    await _sleep(delay)
                    continue
                log.warning("Discord DM channel open for %s -> HTTP %s", user_id, r.status)
                return None
        except Exception as e:
            log.warning("Discord DM channel open for %s failed: %s", user_id, e)
            return None
    return None


async def _send_discord_dm(
    session,
    token,
    user_id,
    text,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
    embed: Optional[dict] = None,
):
    headers = {"Authorization": f"Bot {token}"}
    channel_id = await _open_dm_channel(session, headers, user_id)
    if channel_id is None:
        return False

    chunks = [text] if embed else split_message(text, limit=_DISCORD_CONTENT_LIMIT)
    msg_url = f"{DISCORD_API}/channels/{channel_id}/messages"

    def _body(content: str, first: bool) -> dict:
        body = {"content": content, "allowed_mentions": _ALLOWED_MENTIONS}
        if first and embed:
            body["embeds"] = [embed]
        return body

    async def _post_text(content: str, first: bool = False) -> bool:
        status = await _post_message(session, msg_url, headers, json_body=_body(content, first))
        return status in (200, 201)

    if file_bytes and filename:
        # File goes with the first chunk; any overflow follows as plain messages.
        # Multipart carries the message body in payload_json, which is the only
        # place allowed_mentions can travel alongside an attachment.
        form = aiohttp.FormData()
        form.add_field(
            "payload_json",
            json.dumps(_body(chunks[0], True)),
            content_type="application/json",
        )
        ext = Path(filename).suffix.lower()
        ctype = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".mp4": "video/mp4",
        }.get(ext, "application/octet-stream")
        form.add_field("file", file_bytes, filename=filename, content_type=ctype)
        if await _post_message(session, msg_url, headers, data=form) not in (200, 201):
            return False
        rest = chunks[1:]
    else:
        if not await _post_text(chunks[0], True):
            return False
        rest = chunks[1:]

    for chunk in rest:
        if not await _post_text(chunk):
            return False
    return True


async def _send_all(session, token, subscribers, msg_text, file_bytes, filename, embed=None):
    """Fan out one DM per subscriber, at most _DM_CONCURRENCY at a time."""
    sem = asyncio.Semaphore(_DM_CONCURRENCY)

    async def _one(uid):
        async with sem:
            return await _send_discord_dm(
                session, token, uid, msg_text, file_bytes, filename, embed
            )

    return await asyncio.gather(*[_one(uid) for uid in subscribers], return_exceptions=True)


async def _broadcast_bbnews(
    update: Update,
    content: str,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
):
    token = await _get_discord_token_async()
    if not token:
        await update.message.reply_text("Discord token not found.")
        return
    subscribers = await _get_bongnews_subscribers_async()
    if subscribers is None:
        await update.message.reply_text("Subscriber lookup failed (bbs.db unreadable).")
        return
    if not subscribers:
        await update.message.reply_text("No bongnews subscribers.")
        return

    status_msg = await update.message.reply_text(f"Sending to {len(subscribers)} subscribers..")
    msg_text, embed, upload_name = _news_message(content, filename)

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        results = await _send_all(
            session, token, subscribers, msg_text, file_bytes, upload_name, embed
        )
    ok = sum(1 for r in results if r is True)
    failed_uids = [str(subscribers[i]) for i, r in enumerate(results) if r is not True]

    # Log first: the DMs are out, so a Telegram failure below must not hide the result.
    log.info(f"bbnews sent: ok {ok}, fail {len(failed_uids)}")
    if ok:
        await _save_latest_async(content)

    summary = f"broadcast done: ok {ok} / fail {len(failed_uids)}"
    if failed_uids:
        summary += "\nfailed UIDs: " + ", ".join(failed_uids)
    try:
        await status_msg.edit_text(summary)
    except Exception as e:
        # A raised edit would read as "send failed" and invite a duplicate broadcast.
        log.warning(f"bbnews status edit failed, replying instead: {e}")
        await safe_reply(update.message, summary, parse_mode=None)
