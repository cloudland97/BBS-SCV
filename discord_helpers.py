"""Discord API helpers — token, usernames, DM sending, bongnews broadcast."""

import asyncio
import time
from pathlib import Path
from typing import Optional

import aiohttp
from telegram import Update

from tg_config import (
    DISCORD_API,
    DISCORD_DATA_PATH,
    DISCORD_ENV_PATH,
    log,
    open_db,
)
from tg_utils import split_message

# Cap Discord API calls so a hung request can't stall a broadcast for minutes
# (aiohttp's default total timeout is 300s).
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _get_discord_token() -> Optional[str]:
    try:
        for line in DISCORD_ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("DISCORD_TOKEN="):
                return line.split("=", 1)[1].strip()
    except Exception as e:
        log.warning(f"Discord token unreadable at {DISCORD_ENV_PATH}: {e}")
    return None


_username_cache: dict[str, tuple[str, float]] = {}  # {uid: (name, timestamp)}
_USERNAME_CACHE_TTL = 300  # 5 minutes
_USERNAME_CACHE_MAX = 500  # evict oldest when exceeded


async def _fetch_discord_usernames(uids: list[str]) -> dict[str, str]:
    """Fetch Discord usernames by user ID. Returns {uid: username} dict, ID as fallback.
    Results are cached for 5 minutes to avoid repeated API calls."""
    token = _get_discord_token()
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
    try:
        conn = open_db()  # None when bbs.db is absent — never create a stray file
        if conn is None:
            return []
        try:
            rows = conn.execute("SELECT user_id FROM bongnews_subscribers").fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"bongnews subscribers fetch failed: {e}")
        return []


# Discord rejects a message whose content exceeds 2000 characters with a 400,
# which previously failed the entire broadcast for any long news item.
_DISCORD_CONTENT_LIMIT = 2000


async def _send_discord_dm(
    session,
    token,
    user_id,
    text,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
):
    headers = {"Authorization": f"Bot {token}"}
    async with session.post(
        f"{DISCORD_API}/users/@me/channels",
        json={"recipient_id": str(user_id)},
        headers=headers,
    ) as r:
        if r.status != 200:
            return False
        channel_id = (await r.json())["id"]

    chunks = split_message(text, limit=_DISCORD_CONTENT_LIMIT)

    async def _post_text(content: str) -> bool:
        async with session.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            json={"content": content},
            headers=headers,
        ) as r:
            return r.status in (200, 201)

    if file_bytes and filename:
        # File goes with the first chunk; any overflow follows as plain messages.
        form = aiohttp.FormData()
        form.add_field("content", chunks[0])
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
        async with session.post(
            f"{DISCORD_API}/channels/{channel_id}/messages", data=form, headers=headers
        ) as r:
            if r.status not in (200, 201):
                return False
        rest = chunks[1:]
    else:
        if not await _post_text(chunks[0]):
            return False
        rest = chunks[1:]

    for chunk in rest:
        if not await _post_text(chunk):
            return False
    return True


async def _broadcast_bbnews(
    update: Update,
    content: str,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
):
    token = _get_discord_token()
    if not token:
        await update.message.reply_text("Discord token not found.")
        return
    subscribers = _get_bongnews_subscribers()
    if not subscribers:
        await update.message.reply_text("No bongnews subscribers.")
        return

    # Save full content for feed embed
    try:
        latest_path = DISCORD_DATA_PATH / "bongnews_latest.txt"
        latest_path.write_text(content.strip(), encoding="utf-8")
    except Exception as e:
        log.warning(f"Failed to save bongnews latest: {e}")

    status_msg = await update.message.reply_text(f"Sending to {len(subscribers)} subscribers..")
    msg_text = f"**bongnews**\n\n{content}"

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        results = await asyncio.gather(
            *[
                _send_discord_dm(session, token, uid, msg_text, file_bytes, filename)
                for uid in subscribers
            ],
            return_exceptions=True,
        )
    ok = sum(1 for r in results if r is True)
    failed_uids = [str(subscribers[i]) for i, r in enumerate(results) if r is not True]

    summary = f"broadcast done: ok {ok} / fail {len(failed_uids)}"
    if failed_uids:
        summary += "\nfailed UIDs: " + ", ".join(failed_uids)
    await status_msg.edit_text(summary)
    log.info(f"bbnews sent: ok {ok}, fail {len(failed_uids)}")


async def _broadcast_bbnews_auto(
    bot,
    chat_id: int,
    content: str,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
):
    """Broadcast bongnews without an Update object (scheduled/automated sends)."""
    token = _get_discord_token()
    if not token:
        await bot.send_message(
            chat_id=chat_id, text="❌ 예약 뉴스 발송 실패: Discord token not found."
        )
        return
    subscribers = _get_bongnews_subscribers()
    if not subscribers:
        await bot.send_message(chat_id=chat_id, text="❌ 예약 뉴스 발송 실패: No subscribers.")
        return

    try:
        latest_path = DISCORD_DATA_PATH / "bongnews_latest.txt"
        latest_path.write_text(content.strip(), encoding="utf-8")
    except Exception as e:
        log.warning(f"Failed to save bongnews latest: {e}")

    msg_text = f"**bongnews**\n\n{content}"
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        results = await asyncio.gather(
            *[
                _send_discord_dm(session, token, uid, msg_text, file_bytes, filename)
                for uid in subscribers
            ],
            return_exceptions=True,
        )
    ok = sum(1 for r in results if r is True)
    failed_uids = [str(subscribers[i]) for i, r in enumerate(results) if r is not True]
    summary = f"📤 예약 뉴스 발송 완료: {ok}/{len(subscribers)}"
    if failed_uids:
        summary += "\nfailed: " + ", ".join(failed_uids)
    await bot.send_message(chat_id=chat_id, text=summary)
    log.info(f"scheduled bbnews sent: ok {ok}, fail {len(failed_uids)}")
