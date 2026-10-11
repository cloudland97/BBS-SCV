"""Scheduled-news lifecycle and shared-database bookkeeping.

Covers the send-status honesty fix (R1 / R8), the boot check that could kill the
process (R2), the stuck-'sending' escape hatch (R7), the once-per-process DDL
(N11 / DB-14), the locked-database reply (N12), and the subscription_events
rows the bridge never wrote (N15 / R13).
"""

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import cmd_news_schedule as cns
import cmd_subscribers as cs
import discord_helpers as dh
import telegram_bridge as tb
import tg_config
from tests.fakes import FakeBot as _FakeBot
from tests.fakes import FakeUpdate as _FakeUpdate

UTC = timezone.utc
# str.isdigit() is True for this, int() rejects it. Written as an escape so
# no non-ASCII byte ever lands in a source file (see the repo mojibake rules).
SUPERSCRIPT_TWO = "\u00b2"


class _FakeApp:
    def __init__(self):
        self.bot = _FakeBot()


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bbs.db"
    monkeypatch.setattr(tg_config, "BBS_DB_PATH", db_path)
    cns.reset_ensure_cache()
    cns.reset_send_attempts()
    cns._ensure_table()
    yield db_path
    cns.reset_ensure_cache()
    cns.reset_send_attempts()


def _status(news_id: int) -> str:
    conn = tg_config.get_bbs_db()
    try:
        return conn.execute(
            "SELECT status FROM scheduled_news WHERE id = ?", (news_id,)
        ).fetchone()[0]
    finally:
        conn.close()


# =========================================================
# R7 - release a row stranded in 'sending'
# =========================================================


class TestForceRelease:
    def test_command_releases_only_with_the_force_word(self, temp_db):
        nid = cns.save_scheduled("news", datetime.now(UTC))
        cns.claim_for_send(nid)

        plain = _FakeUpdate(f"!newscc {nid}")
        asyncio.run(cns.cmd_newscc(plain, None))
        assert _status(nid) == "sending"

        forced = _FakeUpdate(f"!newscc {nid} force")
        asyncio.run(cns.cmd_newscc(forced, None))
        assert _status(nid) == "cancelled"
        assert any(f"[{nid}]" in r and "cancelled" in r for r in forced.message.replies)


# =========================================================
# R1 / R8 - the recorded status matches what actually happened
# =========================================================


class TestBootSendStatus:
    def _due_row(self):
        return cns.save_scheduled("scheduled news", datetime.now(UTC) - timedelta(minutes=1))

    def test_successful_send_is_marked_sent(self, temp_db, monkeypatch):
        nid = self._due_row()

        async def _bcast(bot, chat_id, content):
            return (3, 0)

        monkeypatch.setattr(tb, "_broadcast_bbnews_auto", _bcast)
        asyncio.run(tb._boot_scheduled_news(_FakeApp()))
        assert _status(nid) == "sent"

    def test_unattempted_retries_are_capped_then_the_row_goes_terminal(self, temp_db, monkeypatch):
        """A (0, 0) result re-queues, but only for a bounded number of cycles.

        Without the cap, staying 'pending' live-locks the 30s scheduler: it
        reclaims the row forever and the broadcast helper messages the operator
        every cycle. Two retries, then 'failed', then silence.
        """
        nid = self._due_row()
        sends = []

        async def _bcast(bot, chat_id, content):
            sends.append(content)
            return (0, 0)

        monkeypatch.setattr(tb, "_broadcast_bbnews_auto", _bcast)
        app = _FakeApp()

        # Attempts 1 and 2 re-queue.
        for expected in range(1, cns.MAX_UNATTEMPTED_ATTEMPTS):
            asyncio.run(tb._boot_scheduled_news(app))
            assert len(sends) == expected
            assert _status(nid) == "pending"
            assert any(r["id"] == nid for r in cns.get_due(datetime.now(UTC)))

        # Attempt 3 gives up.
        asyncio.run(tb._boot_scheduled_news(app))
        assert len(sends) == cns.MAX_UNATTEMPTED_ATTEMPTS
        assert _status(nid) == "failed"
        assert not any(r["id"] == nid for r in cns.get_due(datetime.now(UTC)))

        # Exactly one message says we stopped, and it names the row.
        giveups = [m for m in app.bot.sent if "`failed`" in m and "!news" in m]
        assert len(giveups) == 1
        assert f"[{nid}]" in giveups[0]

        # Nothing further happens on later cycles.
        asyncio.run(tb._boot_scheduled_news(app))
        assert len(sends) == cns.MAX_UNATTEMPTED_ATTEMPTS
        assert len([m for m in app.bot.sent if "`failed`" in m and "!news" in m]) == 1

    def test_every_dm_failing_is_marked_failed(self, temp_db, monkeypatch):
        nid = self._due_row()

        async def _bcast(bot, chat_id, content):
            return (0, 5)

        monkeypatch.setattr(tb, "_broadcast_bbnews_auto", _bcast)
        asyncio.run(tb._boot_scheduled_news(_FakeApp()))
        # Attempted for every recipient and failed for every one: a real failure.
        assert _status(nid) == "failed"

    def test_a_raised_send_reverts_to_pending_for_retry(self, temp_db, monkeypatch):
        nid = self._due_row()

        async def _bcast(bot, chat_id, content):
            raise RuntimeError("network down")

        monkeypatch.setattr(tb, "_broadcast_bbnews_auto", _bcast)
        asyncio.run(tb._boot_scheduled_news(_FakeApp()))
        assert _status(nid) == "pending"

    @pytest.mark.parametrize(
        "ok,fail,expected",
        [
            (3, 0, "sent"),
            (2, 1, "sent"),
            (0, 5, "failed"),
            (0, 0, "pending"),
        ],
    )
    def test_status_mapping(self, ok, fail, expected):
        cns.reset_send_attempts()
        assert tb._send_status(1, ok, fail) == expected

    def test_budgets_are_tracked_per_row(self):
        cns.reset_send_attempts()
        assert tb._send_status(1, 0, 0) == "pending"
        assert tb._send_status(2, 0, 0) == "pending"
        assert cns._send_attempts == {1: 1, 2: 1}


class TestCancelClearsFailedRows:
    """R3: a 'failed' row is terminal, so !newscc has to be able to clear it."""

    def test_cancel_cancellable_covers_pending_and_failed_only(self, temp_db):
        pending = cns.save_scheduled("a", datetime.now(UTC))
        failed = cns.save_scheduled("b", datetime.now(UTC))
        cns.mark_status(failed, "failed")
        sending = cns.save_scheduled("c", datetime.now(UTC))
        cns.claim_for_send(sending)
        sent = cns.save_scheduled("d", datetime.now(UTC))
        cns.mark_status(sent, "sent")

        assert cns.cancel_cancellable(pending) is True
        assert cns.cancel_cancellable(failed) is True
        assert cns.cancel_cancellable(sending) is False
        assert cns.cancel_cancellable(sent) is False


class TestCompletedSendIsNeverRequeued:
    """S2: once the DMs are out, no later failure may put the row back in the
    queue. A status write that fails after a real broadcast must leave the row
    'sending', which get_due never re-picks."""

    def test_status_write_failure_leaves_the_row_sending(self, temp_db, monkeypatch):
        nid = cns.save_scheduled("news", datetime.now(UTC) - timedelta(minutes=1))

        async def _bcast(bot, chat_id, content):
            return (3, 0)

        async def _mark_boom(news_id, status):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(tb, "_broadcast_bbnews_auto", _bcast)
        monkeypatch.setattr(cns, "a_mark_status", _mark_boom)
        asyncio.run(tb._boot_scheduled_news(_FakeApp()))
        # Not 'pending': reverting would re-send news that already went out.
        assert _status(nid) == "sending"


class TestNewslistShowsFailures:
    def test_get_failed_ignores_every_other_status(self, temp_db):
        pending = cns.save_scheduled("a", datetime.now(UTC))
        sent = cns.save_scheduled("b", datetime.now(UTC))
        cns.mark_status(sent, "sent")
        failed = cns.save_scheduled("c", datetime.now(UTC))
        cns.mark_status(failed, "failed")
        ids = {r["id"] for r in cns.get_failed()}
        assert ids == {failed}
        assert pending not in ids and sent not in ids


# =========================================================
# R2 - the boot check can never stop the bridge from starting
# =========================================================


class TestBootResilience:
    def test_wrapper_absorbs_any_failure(self, monkeypatch):
        async def _boom(app):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(tb, "_boot_scheduled_news", _boom)
        # post_init runs before polling; anything raised here killed the bot.
        assert asyncio.run(tb._check_boot_scheduled_news(_FakeApp())) is None


# =========================================================
# N12 - a locked database is reported, not swallowed
# =========================================================


class TestScheduleSaveFailure:
    def test_locked_database_tells_the_operator(self, temp_db, monkeypatch):
        async def _boom(content, when):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(cns, "a_save_scheduled", _boom)
        update = _FakeUpdate("!news 08:30 morning news")
        asyncio.run(tb._dispatch_text(update, None, "!news 08:30 morning news"))
        assert update.message.replies
        assert update.message.replies[0].startswith("❌ DB")


# =========================================================
# N15 / R13 - !sub writes the audit row the Discord bot writes
# =========================================================


@pytest.fixture
def subs_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bbs.db"
    monkeypatch.setattr(tg_config, "BBS_DB_PATH", db_path)
    conn = tg_config.get_bbs_db()
    # Mirrors the BBS-side schema this bridge shares (utils/database.py).
    conn.executescript(
        """
        CREATE TABLE bongnews_subscribers (user_id INTEGER PRIMARY KEY);
        CREATE TABLE subscription_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            action TEXT NOT NULL,
            ts TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()

    async def _no_lookup(uids):
        return {}

    monkeypatch.setattr(dh, "_fetch_discord_usernames", _no_lookup)
    return db_path


def _events():
    conn = tg_config.get_bbs_db()
    try:
        return [
            (r["user_id"], r["source"], r["action"])
            for r in conn.execute("SELECT user_id, source, action FROM subscription_events")
        ]
    finally:
        conn.close()


class TestSubscriptionEvents:
    UID = 123456789012345678

    def test_add_records_a_subscribe_event(self, subs_db):
        asyncio.run(cs.cmd_sub(_FakeUpdate(f"!sub add {self.UID}"), None))
        assert _events() == [(self.UID, "bongnews", "subscribe")]

    def test_delete_records_an_unsubscribe_event(self, subs_db):
        asyncio.run(cs.cmd_sub(_FakeUpdate(f"!sub add {self.UID}"), None))
        asyncio.run(cs.cmd_sub(_FakeUpdate(f"!sub del {self.UID}"), None))
        assert _events()[-1] == (self.UID, "bongnews", "unsubscribe")
