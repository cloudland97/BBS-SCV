"""Telegram bridge — thin hub that wires handlers and starts polling.

Split modules:
  tg_config.py      — env, paths, constants, mutable globals
  tg_utils.py       — message splitting, HUD, formatting helpers
  discord_helpers.py — Discord API helpers, username cache, DM, bongnews
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from tg_config import (
    ALLOWED_USER_ID,
    BBS_DB_PATH,
    BOT_TOKEN,
    LOG_DIR,
    LOOP_ALERT_COOLDOWN_SEC,
    LOOP_ALERT_THRESHOLDS,
    PROJECTS,
    check_shared_schema,
    get_bbs_db,
    log,
)
from tg_utils import ensure_utc
from discord_helpers import _broadcast_bbnews, _broadcast_bbnews_auto
import cmd_news_schedule as _cns


# -- Loop monitor --

_loop_alert_last: dict[str, float] = {}


async def _loop_monitor_task(app):
    """Background task: alert if a loop hasn't run within its threshold."""
    await asyncio.sleep(60)
    while True:
        await asyncio.sleep(300)
        if not BBS_DB_PATH.exists():
            continue
        try:
            now_utc = datetime.now(timezone.utc)
            conn = get_bbs_db()
            try:
                rows = conn.execute("SELECT loop_id, last_run FROM loop_health").fetchall()
            finally:
                conn.close()

            alerts = []
            for row in rows:
                loop_id = row["loop_id"]
                threshold_mins = LOOP_ALERT_THRESHOLDS.get(loop_id, 180)
                last_dt = ensure_utc(datetime.fromisoformat(row["last_run"]))
                diff_mins = (now_utc - last_dt).total_seconds() / 60
                if diff_mins > threshold_mins:
                    last_alerted = _loop_alert_last.get(loop_id, 0)
                    if time.time() - last_alerted > LOOP_ALERT_COOLDOWN_SEC:
                        _loop_alert_last[loop_id] = time.time()
                        h, m = int(diff_mins // 60), int(diff_mins % 60)
                        age = f"{h}h{m}m" if h else f"{m}m"
                        alerts.append(f"⚠️ `{loop_id}` — {age} no response")

            if alerts:
                msg = "Loop down detected\n\n" + "\n".join(alerts)
                await app.bot.send_message(
                    chat_id=ALLOWED_USER_ID, text=msg, parse_mode="Markdown"
                )
                log.warning(f"Loop alert: {alerts}")
        except Exception as e:
            log.error(f"Loop monitor error: {e}")


# -- News scheduler --


async def _check_boot_scheduled_news(app):
    """On boot: send news due within 15 min; expire and notify anything older.

    Also detect rows orphaned in 'sending' by a crash mid-send and warn the admin.
    This is detect-and-notify only — never auto-resend, because a crash AFTER a
    successful broadcast (but before mark 'sent') would otherwise double-send.
    """
    now_utc = datetime.now(timezone.utc)
    KST = ZoneInfo("Asia/Seoul")

    # Stale-'sending' recovery: rows claimed >5 min ago that never reached 'sent'.
    # A human decides whether they actually went out; we only surface them.
    stale = _cns.get_stale_sending(now_utc - timedelta(minutes=5))
    if stale:
        lines = [f"⚠️ 발송 중(sending) 상태로 멈춘 예약 뉴스 {len(stale)}건 — 발송 여부 수동 확인 필요:"]
        for item in stale:
            claimed_kst = datetime.fromisoformat(item["claimed_at"]).replace(tzinfo=timezone.utc).astimezone(KST)
            lines.append(f"• `[{item['id']}]` {claimed_kst.strftime('%m/%d %H:%M')} claimed — {item['content'][:30]}...")
        await app.bot.send_message(chat_id=ALLOWED_USER_ID, text="\n".join(lines), parse_mode="Markdown")

    cutoff = now_utc - timedelta(minutes=15)
    due = _cns.get_due(now_utc)
    if not due:
        return

    send_now, expired = [], []
    for item in due:
        fired_at = datetime.fromisoformat(item["scheduled_at"]).replace(tzinfo=timezone.utc)
        (send_now if fired_at >= cutoff else expired).append(item)

    if expired:
        lines = [f"⚠️ 봇 다운 중 만료된 예약 뉴스 {len(expired)}건:"]
        for item in expired:
            dt_kst = datetime.fromisoformat(item["scheduled_at"]).replace(tzinfo=timezone.utc).astimezone(KST)
            lines.append(f"• `[{item['id']}]` {dt_kst.strftime('%m/%d %H:%M')} — {item['content'][:30]}...")
            _cns.mark_status(item["id"], "expired")
        await app.bot.send_message(chat_id=ALLOWED_USER_ID, text="\n".join(lines), parse_mode="Markdown")

    for item in send_now:
        # Same claim-before-send guard as the 30s loop (the scheduler task also
        # starts on boot, so both could race the same due row).
        if not _cns.claim_for_send(item["id"]):
            continue
        try:
            await _broadcast_bbnews_auto(app.bot, ALLOWED_USER_ID, item["content"])
            _cns.mark_status(item["id"], "sent")
        except Exception as e:
            # Send did not complete -> back to 'pending' for retry; a post-send
            # mark failure leaves it 'sending' (not re-sent) -> no duplicate.
            _cns.mark_status(item["id"], "pending")
            log.error(f"Boot scheduled news send failed id={item['id']}: {e}")


async def _news_scheduler_task(app):
    """Background task: fire scheduled news when due (checks every 30s)."""
    await asyncio.sleep(30)
    while True:
        await asyncio.sleep(30)
        if not BBS_DB_PATH.exists():
            continue  # standalone clone / BBS bot not set up yet
        try:
            due = _cns.get_due(datetime.now(timezone.utc))
            for item in due:
                # Claim BEFORE broadcasting: only one winner flips 'pending' ->
                # 'sending', so get_due won't re-pick this row next cycle -> no
                # duplicate. A lost claim means another cycle already has it.
                if not _cns.claim_for_send(item["id"]):
                    continue
                log.info(f"Sending scheduled news id={item['id']}")
                try:
                    await _broadcast_bbnews_auto(app.bot, ALLOWED_USER_ID, item["content"])
                    _cns.mark_status(item["id"], "sent")
                except Exception as e:
                    # Send did NOT complete -> revert to 'pending' so it retries.
                    # If mark_status('sent') above fails after a real send, the row
                    # stays 'sending' (not reverted here) and is never re-sent.
                    _cns.mark_status(item["id"], "pending")
                    log.error(f"Scheduled news send failed id={item['id']}: {e}")
        except Exception as e:
            log.error(f"News scheduler error: {e}")


# -- Photo / video / document handlers --


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    if caption.startswith("!news2"):
        await _handle_news_media(update, context, caption, update.message.photo[-1])
        return
    await update.message.reply_text(
        "Use `!news2 content` caption to broadcast photo via bongnews."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    if caption.startswith("!news2"):
        await _handle_news_media(update, context, caption, update.message.document)
        return
    await update.message.reply_text(
        "Use `!news2 content` caption to broadcast document via bongnews."
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    if caption.startswith("!news2"):
        await _handle_news_media(update, context, caption, update.message.video)
        return
    await update.message.reply_text(
        "Use `!news2 content` caption to broadcast video via bongnews."
    )


# -- Bongnews commands --


async def cmd_bbnews_text(update: Update, content: str):
    """Handle !news text command."""
    content = content.replace("\\n", "\n")
    if not content:
        await update.message.reply_text(
            "Usage:\n"
            "• `!news content` — text only\n"
            "• `!news2 content` — with media",
            parse_mode="Markdown",
        )
        return
    await _broadcast_bbnews(update, content)


async def cmd_bbnews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /bbnews slash command."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    content = " ".join(context.args).strip()
    await cmd_bbnews_text(update, content)


async def _handle_news_media(
    update: Update, context: ContextTypes.DEFAULT_TYPE, caption: str, media
):
    """Handle !news2 media — download from Telegram and broadcast to Discord."""
    content = caption[len("!news2") :].strip().replace("\\n", "\n")
    tg_file = await context.bot.get_file(media.file_id)
    fname = getattr(media, "file_name", None)
    if not fname:
        ext = ".mp4" if hasattr(media, "duration") else ".jpg"
        fname = f"media_{time.time_ns()}{ext}"
    tmp = Path(PROJECTS["discord"]) / f"_tg_news_{time.time_ns()}_{fname}"
    await tg_file.download_to_drive(str(tmp))
    try:
        file_bytes = tmp.read_bytes()
        await _broadcast_bbnews(update, content, file_bytes, tmp.name)
    finally:
        try:
            tmp.unlink()
        except Exception as e:
            log.debug(f"cleanup failed: {e}")


# -- Main message handler --


async def _dispatch_text(update: Update, context: ContextTypes.DEFAULT_TYPE, t: str):
    """Dispatch a text command using prefix table (longer/specific entries first)."""
    # Special cases that need local argument parsing
    if t.startswith("!newslist"):
        await _cns.cmd_newslist(update, context)
        return
    if t.startswith("!newscc"):
        await _cns.cmd_newscc(update, context)
        return
    if t.startswith("!news2"):
        await cmd_bbnews_text(update, t[len("!news2"):].strip())
        return
    if t.startswith("!news"):
        raw = t[len("!news"):].strip()
        try:
            scheduled_at, content = _cns.parse_schedule_time(raw)
            if not content:
                await update.message.reply_text("내용을 입력해주세요.")
                return
            news_id = _cns.save_scheduled(content, scheduled_at)
            dt_kst = scheduled_at.astimezone(ZoneInfo("Asia/Seoul"))
            await update.message.reply_text(
                f"✅ 예약됨 `[{news_id}]`\n"
                f"📅 {dt_kst.strftime('%m/%d(%a) %H:%M')} KST\n"
                f"📝 {content[:60]}",
                parse_mode="Markdown",
            )
        except ValueError as e:
            if str(e) == _cns._NO_TIME:
                await cmd_bbnews_text(update, raw)
            else:
                await update.message.reply_text(
                    f"❌ {e}\n\n{_cns.TIME_FORMAT_HELP}", parse_mode="Markdown"
                )
        return
    # Exact first-token lookup: the command word is matched directly, so a short
    # command can no longer shadow a longer one that shares its prefix
    # (e.g. "!banlist" is no longer intercepted by "!ban").
    cmd = t.split(maxsplit=1)[0]
    handler = _CMD_TABLE.get(cmd)
    if handler is not None:
        await handler(update, context)
        return

    # Fallback for attached-argument forms with no space (e.g. "!log1", "!st7d"):
    # preserve the historical longest-prefix-first startswith behavior.
    for prefix, h in _CMD_ROUTES:
        if t.startswith(prefix):
            await h(update, context)
            return

    await update.message.reply_text("Unknown command. `!help` 참조.", parse_mode="Markdown")


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "!chatid":
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        await update.message.reply_text(f"chat_id: `{chat_id}`\nuser_id: `{user_id}`", parse_mode="Markdown")
        return
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("No permission")
        return
    if update.message.photo:
        await handle_photo(update, context)
    elif update.message.video:
        await handle_video(update, context)
    elif update.message.document:
        await handle_document(update, context)
    else:
        await _dispatch_text(update, context, (update.message.text or "").strip())


# -- Startup --


def _cleanup_old_logs(days: int = 30):
    """Delete log files older than N days on startup."""
    cutoff = time.time() - days * 86400
    for f in LOG_DIR.glob("bridge_*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                log.info("Deleted old log: %s", f.name)
        except Exception as e:
            log.debug(f"cleanup failed: {e}")


def _cleanup_orphan_temp(max_age_sec: int = 3600):
    """Delete orphaned _tg_* download temp files left behind by crashes."""
    cutoff = time.time() - max_age_sec
    for proj_dir in {Path(p) for p in PROJECTS.values()}:
        if not proj_dir.exists():
            continue
        for f in proj_dir.glob("_tg_*"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    log.info("Deleted orphan temp: %s", f.name)
            except Exception as e:
                log.debug(f"orphan cleanup failed: {e}")


# Strong refs to the background tasks: asyncio holds tasks weakly, so an
# unreferenced create_task result can be garbage collected mid-run — the exact
# "bridge goes silent" failure class this repo has hit before.
_bg_tasks: list = []


async def post_init(application):
    _cleanup_old_logs()
    _cleanup_orphan_temp()
    check_shared_schema()
    _bg_tasks.append(asyncio.create_task(_loop_monitor_task(application)))
    _bg_tasks.append(asyncio.create_task(_news_scheduler_task(application)))
    if BBS_DB_PATH.exists():
        await _check_boot_scheduled_news(application)
    else:
        # A standalone clone has no sibling BBS repo; connecting anyway would
        # raise inside post_init and kill the bot before polling ever starts.
        log.warning("bbs.db not found - boot scheduled-news check skipped")
    log.info("Telegram bridge starting")


import cmd_claude as _cc  # noqa: E402
import cmd_discord as _cd  # noqa: E402
import cmd_subscribers as _cs  # noqa: E402
import cmd_moderation as _cm  # noqa: E402

# Dispatch table for text commands — longer/more-specific prefixes must come first
# to prevent shorter prefixes from shadowing them (e.g. "!banlist" before "!ban").
_CMD_ROUTES: list[tuple[str, object]] = [
    ("!re0",     _cm.cmd_restart),
    ("!re",      _cd.cmd_drestart),
    ("!sync",    _cd.cmd_sync),
    ("!banlist", _cm.cmd_banlist),
    ("!unban",   _cm.cmd_unban),
    ("!ban",     _cm.cmd_ban),
    ("!list",    _cs.cmd_list),
    ("!help",    _cc.cmd_help),
    ("!st",      _cm.cmd_stats),
    ("!dm",      _cd.cmd_dm),
    ("!hp",      _cd.cmd_hp),
    ("!log",     _cd.cmd_log),
    ("!ud",      _cd.cmd_dm_log),
    ("!sub",     _cs.cmd_sub),
    ("!se",      _cs.cmd_sub_events),
    ("!today",   _cd.cmd_today),
]

# Exact-match table built from the ordered routes above. The list is kept as the
# fallback source of truth for attached-argument (spaceless) command forms.
_CMD_TABLE: dict[str, object] = dict(_CMD_ROUTES)


async def _cmd_help_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cc.cmd_help(update, context)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Quiet transient polling errors; full traceback only for real bugs."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        # Telegram-side hiccup (Bad Gateway, timeout) — PTB auto-retries.
        log.warning(f"Transient network error (auto-retry): {err}")
        return
    log.error("Unhandled exception in handler", exc_info=err)


_req = HTTPXRequest(
    connect_timeout=20.0,
    read_timeout=20.0,
    write_timeout=20.0,
    httpx_kwargs={
        "transport": httpx.AsyncHTTPTransport(retries=2, local_address="0.0.0.0"),
    },
)
app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .request(_req)
    .get_updates_request(_req)
    .post_init(post_init)
    .build()
)
app.add_error_handler(_on_error)
app.add_handler(CommandHandler("help", _cmd_help_entry))
app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND
        | filters.PHOTO
        | filters.VIDEO
        | filters.Document.ALL,
        handle,
    )
)
if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    print("telegram bridge running...")
    app.run_polling()
