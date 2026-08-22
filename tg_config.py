"""Telegram bridge configuration — env loading, paths, constants."""

import logging
import os
import random
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        _file_handler,
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("telegram_bridge")


# -- Env loading --


def _load_tg_env() -> tuple[str, int, dict]:
    env_file = Path(__file__).parent / "tg.env"
    token, chat_id = None, None
    extras: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("BOT_TOKEN="):
                token = line.split("=", 1)[1].strip()
            elif line.startswith("CHAT_ID="):
                chat_id = int(line.split("=", 1)[1].strip())
            elif "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                extras[k.strip()] = v.strip()
    # Env-var fallback so tests and CI can import this module without a real
    # tg.env (a fresh clone has none; the error below used to kill collection).
    token = token or os.getenv("TG_BOT_TOKEN")
    if not chat_id and os.getenv("TG_CHAT_ID"):
        chat_id = int(os.getenv("TG_CHAT_ID"))
    if not token:
        raise RuntimeError("BOT_TOKEN not found in tg.env")
    if not chat_id:
        raise RuntimeError("CHAT_ID not found in tg.env")
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
