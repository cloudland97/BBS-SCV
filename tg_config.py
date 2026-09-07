"""Telegram bridge configuration — env loading, paths, constants."""

import asyncio
import logging
import os
import random
import re
import sqlite3
import time
from datetime import timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

# Korea Standard Time (UTC+9), fixed offset. Single source of truth for the
# fixed-offset KST used across command modules (hoisted from per-module copies).
KST = timezone(timedelta(hours=9))

# -- Logging setup --

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _log_namer(default_name: str) -> str:
    """Render rotated log names as bridge_<date>.log (not bridge.log.<date>).

    Keeps the historical bridge_*.log naming so _cleanup_old_logs' glob and
    existing tooling keep matching rotated files.
    """
    base, _, date = default_name.rpartition(".log.")
    return f"{base}_{date}.log" if date else default_name


# TimedRotatingFileHandler rotates at midnight, so a long-running process no
# longer keeps writing to yesterday's file after the date rolls over.
_file_handler = TimedRotatingFileHandler(
    LOG_DIR / "bridge.log",
    when="midnight",
    backupCount=30,
    encoding="utf-8",
)
_file_handler.suffix = "%Y-%m-%d"
_file_handler.namer = _log_namer

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        _file_handler,
        logging.StreamHandler(),
    ],
)
# The polling loop asks Telegram for updates every ~10s and httpx logs each
# request at INFO, which drowns the log: a sample day held 8,428 lines, of
# which 8,408 were getUpdates. Only their warnings are worth keeping - a real
# network problem still shows up, and this bridge's own INFO lines stay.
for _noisy in ("httpx", "httpcore", "telegram.ext.Updater", "telegram.request"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("telegram_bridge")


# -- Secret redaction --

# A Telegram bot token travels in the API path (api.telegram.org/bot<TOKEN>/...).
# httpx logged every request at INFO until 2026-08-24, which wrote roughly
# 166,000 live-token lines into logs/. Lowering that logger's level fixed the
# known emitter, but any library that logs a request URL or an exception
# carrying one can do it again - so redact at the handler, not at the call site.
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_TOKEN_MARKER = "bot<redacted>"


def redact_tokens(text: str) -> str:
    """Replace any Telegram bot token in *text* with a marker."""
    return _TOKEN_RE.sub(_TOKEN_MARKER, text)


class TokenRedactingFilter(logging.Filter):
    """Strip bot tokens out of a record's message before it reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact_tokens(message)
        if redacted != message:
            # Render once here; args are consumed so downstream formatting is a
            # no-op and cannot reintroduce the raw value.
            record.msg = redacted
            record.args = ()
        return True


class TokenRedactingFormatter(logging.Formatter):
    """Formatter that also scrubs the formatted traceback.

    The filter only sees the message; a token inside exc_info (httpx puts the
    full URL in its exception text) is rendered later, so it needs this pass.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_tokens(super().format(record))


def install_token_redaction(*handlers: logging.Handler) -> None:
    """Attach the redacting filter and formatter to *handlers*.

    Called with the root handlers at import time; safe to call again (a handler
    keeps one filter instance per call, which is idempotent in effect).
    """
    for handler in handlers:
        handler.addFilter(TokenRedactingFilter())
        existing = handler.formatter
        handler.setFormatter(
            TokenRedactingFormatter(
                fmt=getattr(existing, "_fmt", None) or LOG_FORMAT,
                datefmt=getattr(existing, "datefmt", None),
            )
        )


install_token_redaction(*logging.getLogger().handlers)


# -- Env loading --


def _parse_chat_id(raw: str, origin: str) -> int:
    """Parse CHAT_ID, reporting what is wrong instead of a bare ValueError.

    A non-numeric value used to surface as `invalid literal for int()` with no
    hint about which setting or file was at fault.
    """
    raw = (raw or "").strip()
    if not raw:
        raise RuntimeError(
            f"CHAT_ID in {origin} is empty - set it to your numeric Telegram user id."
        )
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            f"CHAT_ID in {origin} must be a number, got {raw!r} - "
            "use the numeric id that !chatid reports, not a @username."
        ) from None


def _load_tg_env() -> tuple[str, int, dict]:
    env_file = Path(__file__).parent / "tg.env"
    token, chat_id = None, None
    extras: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("BOT_TOKEN="):
                token = line.split("=", 1)[1].strip()
            elif line.startswith("CHAT_ID="):
                chat_id = _parse_chat_id(line.split("=", 1)[1], str(env_file))
            elif "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                extras[k.strip()] = v.strip()
    # Env-var fallback so tests and CI can import this module without a real
    # tg.env (a fresh clone has none; the error below used to kill collection).
    token = token or os.getenv("TG_BOT_TOKEN")
    if chat_id is None and os.getenv("TG_CHAT_ID"):
        chat_id = _parse_chat_id(os.getenv("TG_CHAT_ID"), "the TG_CHAT_ID env var")
    if not token:
        raise RuntimeError(f"BOT_TOKEN not found in {env_file} or the TG_BOT_TOKEN env var.")
    # `not chat_id` also rejected a literal 0, which then read as "not found".
    if chat_id is None:
        raise RuntimeError(f"CHAT_ID not found in {env_file} or the TG_CHAT_ID env var.")
    if chat_id == 0:
        raise RuntimeError("CHAT_ID is 0, which is not a valid Telegram user id.")
    return token, chat_id, extras


BOT_TOKEN, ALLOWED_USER_ID, _tg_extras = _load_tg_env()

# Single project root: the BBS discord bot (Claude bridge's multi-project
# switching was removed 2026-07-07; "all"/"bot" keys had no other consumers).
PROJECTS = {
    "discord": _tg_extras.get("PROJECTS_DISCORD", r"C:\DEV\BBS"),
}

_discord_root = Path(PROJECTS["discord"])
DISCORD_ENV_PATH = _discord_root / "bss.env"
DISCORD_DATA_PATH = _discord_root / "data"
DISCORD_LOGS_PATH = _discord_root / "logs"
BBS_DB_PATH = DISCORD_DATA_PATH / "bbs.db"
DISCORD_API = "https://discord.com/api/v10"


def get_bbs_db():
    """Open a SQLite connection to bbs.db with WAL mode and timeout."""
    conn = sqlite3.connect(str(BBS_DB_PATH), timeout=4)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def open_db(db_path: Optional[Path] = None):
    """Return a bbs.db connection (WAL), or None if the DB file is missing.

    Hoisted here from three identical per-module _open_db copies so command
    modules share one definition.
    """
    if db_path is None:
        db_path = BBS_DB_PATH
    if not db_path.exists():
        return None
    return get_bbs_db()


# Tables owned by the BBS discord bot that this bridge reads from. scheduled_news
# is intentionally excluded (this bridge creates it itself via _ensure_table).
EXPECTED_SHARED_TABLES = (
    "loop_health",
    "loop_health_daily",
    "command_usage",
    "user_dm",
    "temp_bans",
    "bongnews_subscribers",
    "subscribers",
    "market_subscribers",
    "ark_subscribers",
    "morning_subscribers",
    "danawa_subscriptions",
    "pubg_subscribers",
    "subscription_events",
)


def check_shared_schema() -> list[str]:
    """One-time boot sanity check: warn if expected shared tables are missing.

    A BBS-side migration that renames/drops a table would otherwise surface as a
    raw sqlite3.OperationalError to the user mid-command. This only detects and
    logs; it never creates or migrates tables. Returns the missing table names.
    """
    if not BBS_DB_PATH.exists():
        log.warning("Shared schema check skipped: bbs.db not found at %s", BBS_DB_PATH)
        return []
    try:
        conn = get_bbs_db()
        try:
            present = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.warning("Shared schema check failed to read bbs.db: %s", e)
        return []
    missing = [t for t in EXPECTED_SHARED_TABLES if t not in present]
    if missing:
        log.warning(
            "Shared schema drift: %d expected bbs.db table(s) missing: %s "
            "(BBS-side migration?) — related commands may error.",
            len(missing),
            ", ".join(missing),
        )
    return missing


def _is_lock_error(e):
    msg = str(e).lower()
    return "locked" in msg or "busy" in msg


def db_write(fn, max_attempts=5, base_sleep=0.05, max_total=8.0):
    """Run fn(conn) inside a fresh bbs.db transaction, commit on success, and
    retry the WHOLE call on SQLITE_BUSY/locked. Returns fn's return value.
    Only lock/busy OperationalErrors are retried; everything else propagates."""
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        conn = get_bbs_db()
        try:
            result = fn(conn)
            conn.commit()
            return result
        except sqlite3.OperationalError as e:
            conn.rollback()
            if (
                (not _is_lock_error(e))
                or attempt >= max_attempts
                or (time.monotonic() - start) >= max_total
            ):
                raise
            sleep = min(base_sleep * (2 ** (attempt - 1)), 1.0)
            time.sleep(sleep + random.uniform(0, base_sleep))
        finally:
            conn.close()


async def db_write_async(fn, **kwargs):
    """Await db_write off the event loop.

    db_write retries SQLITE_BUSY with time.sleep (up to 8s). Run inline in a
    handler that stalled the whole polling loop whenever the BBS bot held the
    shared database.
    """
    return await asyncio.to_thread(db_write, fn, **kwargs)


def db_read(fn):
    """Run a read-only fn(conn) on a fresh connection and always close it."""
    conn = get_bbs_db()
    try:
        return fn(conn)
    finally:
        conn.close()


async def db_read_async(fn):
    """Await db_read off the event loop (see db_write_async)."""
    return await asyncio.to_thread(db_read, fn)


async def fetch_rows_async(sql: str, params: tuple = ()) -> list:
    """Run one SELECT off the event loop on a fresh connection and return the rows.

    Every handler read goes through here (or db_read_async), so a BBS-side
    write holding the shared bbs.db can no longer stall polling for the
    connect timeout.
    """
    return await db_read_async(lambda conn: conn.execute(sql, params).fetchall())


# Slash commands to track for !st
TRACKED_COMMANDS = [
    "bbtt",
    "bbf1",
    "bbmk",
    "bbark",
    "bbup",
    "bbs",
    "bbday",
    "bbfb",
    "bbpf",
    "bbbg",
]

# Loop monitor thresholds (minutes)
LOOP_ALERT_THRESHOLDS = {
    "market_loop": 120,
    "danawa_loop": 180,
    "morning_loop": 180,
    "feed_loop": 60,
    "pubg_daily_loop": 1440,
    "live_score_loop": 360,
    "notify_loop": 360,
    "lineup_loop": 360,
    "result_loop": 360,
    "f1_standings_loop": 120,
    "f1_result_loop": 120,
    "rate_alert_loop": 1440,
    # ark_loop fires once a day at 20:00 KST, so last_run is always up to 24h old.
    # With no entry here it fell to the 180-minute default and reported "no
    # response" for most of every day. 26h leaves headroom for a restart.
    "ark_loop": 1560,
    # etf_holdings_loop is likewise once-daily (22:00 KST); same 26h reasoning.
    "etf_holdings_loop": 1560,
}
LOOP_ALERT_COOLDOWN_SEC = 6 * 3600  # 6h per loop
