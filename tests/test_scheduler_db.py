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
# N11 / DB-14 - the shared DB stops taking a DDL commit every 30s
# =========================================================


class TestEnsureTableCaching:
    def test_ddl_runs_once_per_database(self, temp_db, monkeypatch):
        cns.reset_ensure_cache()
        calls = []
        real = cns.db_write

        def _counting(fn, **kw):
            calls.append(fn)
            return real(fn, **kw)

        monkeypatch.setattr(cns, "db_write", _counting)
        cns._ensure_table()
        cns._ensure_table()
        cns.get_pending()
        cns.get_due(datetime.now(UTC))
        assert len(calls) == 1

    def test_a_different_database_still_gets_its_ddl(self, temp_db, tmp_path, monkeypatch):
        other = tmp_path / "other.db"
        monkeypatch.setattr(tg_config, "BBS_DB_PATH", other)
        cns._ensure_table()
        conn = tg_config.get_bbs_db()
        try:
            names = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
        assert "scheduled_news" in names


# =========================================================
# R7 - release a row stranded in 'sending'
# =========================================================


class TestForceRelease:
    def test_releases_a_stuck_sending_row(self, temp_db):
        nid = cns.save_scheduled("news", datetime.now(UTC))
        assert cns.claim_for_send(nid) is True
        assert cns.force_release(nid) is True
        assert _status(nid) == "cancelled"

    def test_leaves_a_pending_row_alone(self, temp_db):
        nid = cns.save_scheduled("news", datetime.now(UTC))
        assert cns.force_release(nid) is False
        assert _status(nid) == "pending"

    def test_is_idempotent(self, temp_db):
        nid = cns.save_scheduled("news", datetime.now(UTC))
        cns.claim_for_send(nid)
        assert cns.force_release(nid) is True
        assert cns.force_release(nid) is False

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

    def test_force_on_a_healthy_row_says_so(self, temp_db):
        nid = cns.save_scheduled("news", datetime.now(UTC))
        update = _FakeUpdate(f"!newscc {nid} force")
        asyncio.run(cns.cmd_newscc(update, None))
        assert any(f"ID {nid}:" in r and "sending" in r for r in update.message.replies)
        assert _status(nid) == "pending"

    def test_non_ascii_digits_get_usage_not_a_traceback(self, temp_db):
        # R19: SUPERSCRIPT TWO passes str.isdigit() but int() rejects it.
        update = _FakeUpdate(f"!newscc {SUPERSCRIPT_TWO}")
        asyncio.run(cns.cmd_newscc(update, None))
        assert update.message.replies and "Usage" in update.message.replies[0]


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

    def test_a_send_that_was_never_attempted_stays_pending(self, temp_db, monkeypatch):
        """R6: (0, 0) means no Discord token or no subscribers, so nothing was
        ever attempted. Recording a delivery failure for it would be a lie; the
        row waits for the next cycle instead."""
        nid = self._due_row()

        async def _bcast(bot, chat_id, content):
            return (0, 0)

        monkeypatch.setattr(tb, "_broadcast_bbnews_auto", _bcast)
        asyncio.run(tb._boot_scheduled_news(_FakeApp()))
        assert _status(nid) == "pending"

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

    def test_the_retry_budget_is_returned_when_a_send_succeeds(self, temp_db, monkeypatch):
        """A row that fails once then works must not carry the old count."""
        nid = self._due_row()
        outcome = {"value": (0, 0)}

        async def _bcast(bot, chat_id, content):
            return outcome["value"]

        monkeypatch.setattr(tb, "_broadcast_bbnews_auto", _bcast)
        asyncio.run(tb._boot_scheduled_news(_FakeApp()))
        assert cns._send_attempts.get(nid) == 1

        outcome["value"] = (2, 0)
        asyncio.run(tb._boot_scheduled_news(_FakeApp()))
        assert _status(nid) == "sent"
        assert nid not in cns._send_attempts

    def test_cancelling_stops_the_retry_loop_and_clears_the_budget(self, temp_db, monkeypatch):
        """!newscc is the operator's way out before the cap is reached."""
        nid = self._due_row()

        async def _bcast(bot, chat_id, content):
            return (0, 0)

        monkeypatch.setattr(tb, "_broadcast_bbnews_auto", _bcast)
        asyncio.run(tb._boot_scheduled_news(_FakeApp()))
        assert cns._send_attempts.get(nid) == 1

        asyncio.run(cns.cmd_newscc(_FakeUpdate(f"!newscc {nid}"), None))
        assert _status(nid) == "cancelled"
        assert not any(r["id"] == nid for r in cns.get_due(datetime.now(UTC)))
        assert nid not in cns._send_attempts

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

    def test_a_failed_row_is_never_picked_up_again(self, temp_db, monkeypatch):
        nid = self._due_row()

        async def _bcast(bot, chat_id, content):
            return (0, 5)

        monkeypatch.setattr(tb, "_broadcast_bbnews_auto", _bcast)
        asyncio.run(tb._boot_scheduled_news(_FakeApp()))
        assert not any(r["id"] == nid for r in cns.get_due(datetime.now(UTC)))

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

    def test_newscc_cancels_a_failed_row(self, temp_db):
        nid = cns.save_scheduled("news", datetime.now(UTC))
        cns.mark_status(nid, "failed")
        update = _FakeUpdate(f"!newscc {nid}")
        asyncio.run(cns.cmd_newscc(update, None))
        assert _status(nid) == "cancelled"

    def test_newscc_still_cancels_a_pending_row(self, temp_db):
        nid = cns.save_scheduled("news", datetime.now(UTC))
        update = _FakeUpdate(f"!newscc {nid}")
        asyncio.run(cns.cmd_newscc(update, None))
        assert _status(nid) == "cancelled"

    def test_newscc_still_refuses_a_sending_row(self, temp_db):
        nid = cns.save_scheduled("news", datetime.now(UTC))
        cns.claim_for_send(nid)
        update = _FakeUpdate(f"!newscc {nid}")
        asyncio.run(cns.cmd_newscc(update, None))
        # Only the explicit `force` word may touch a row that may be in flight.
        assert _status(nid) == "sending"

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

    def test_a_requeued_row_is_only_one_the_send_never_completed_for(self, temp_db, monkeypatch):
        nid = cns.save_scheduled("news", datetime.now(UTC) - timedelta(minutes=1))

        async def _bcast(bot, chat_id, content):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(tb, "_broadcast_bbnews_auto", _bcast)
        asyncio.run(tb._boot_scheduled_news(_FakeApp()))
        assert _status(nid) == "pending"


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

    def test_a_broken_telegram_send_does_not_abort_the_check(self, temp_db, monkeypatch):
        from telegram.error import BadRequest

        nid = cns.save_scheduled("news", datetime.now(UTC) - timedelta(minutes=1))

        class _BrokenBot:
            def __init__(self):
                self.plain = []

            async def send_message(self, chat_id, text, parse_mode=None, **_kw):
                if parse_mode:
                    raise BadRequest("can't parse entities")
                self.plain.append(text)

        app = type("A", (), {"bot": _BrokenBot()})()

        async def _bcast(bot, chat_id, content):
            return (1, 0)

        monkeypatch.setattr(tb, "_broadcast_bbnews_auto", _bcast)
        asyncio.run(tb._check_boot_scheduled_news(app))
        assert _status(nid) == "sent"


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

    def test_a_normal_schedule_still_confirms(self, temp_db):
        update = _FakeUpdate("!news 08:30 morning news")
        asyncio.run(tb._dispatch_text(update, None, "!news 08:30 morning news"))
        assert update.message.replies
        assert cns.get_pending()


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

    def test_a_no_op_add_records_nothing(self, subs_db):
        asyncio.run(cs.cmd_sub(_FakeUpdate(f"!sub add {self.UID}"), None))
        asyncio.run(cs.cmd_sub(_FakeUpdate(f"!sub add {self.UID}"), None))
        assert len(_events()) == 1

    def test_a_no_op_delete_records_nothing(self, subs_db):
        asyncio.run(cs.cmd_sub(_FakeUpdate(f"!sub del {self.UID}"), None))
        assert _events() == []

    def test_a_missing_events_table_does_not_lose_the_subscription(self, subs_db, monkeypatch):
        """S3: subscription_events is the BBS bot's schema, not ours. A drift
        there must cost an audit row, never the membership change the operator
        actually asked for."""
        conn = tg_config.get_bbs_db()
        try:
            conn.execute("DROP TABLE subscription_events")
            conn.commit()
        finally:
            conn.close()

        update = _FakeUpdate(f"!sub add {self.UID}")
        asyncio.run(cs.cmd_sub(update, None))

        conn = tg_config.get_bbs_db()
        try:
            rows = conn.execute(
                "SELECT user_id FROM bongnews_subscribers WHERE user_id = ?", (self.UID,)
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        # The operator is told it worked, because it did.
        assert update.message.replies

    def test_a_pasted_discord_mention_is_accepted(self, subs_db):
        asyncio.run(cs.cmd_sub(_FakeUpdate(f"!sub add <@{self.UID}>"), None))
        assert _events() == [(self.UID, "bongnews", "subscribe")]

    def test_non_ascii_digits_get_usage_not_a_traceback(self, subs_db):
        update = _FakeUpdate(f"!sub add {SUPERSCRIPT_TWO * 3}")
        asyncio.run(cs.cmd_sub(update, None))
        assert _events() == []
        assert update.message.replies


# =========================================================
# R14 - the !se window boundary matches the stored timestamp format
# =========================================================


class TestEventsCutoff:
    def test_cutoff_has_the_shape_sqlite_stores(self, subs_db):
        conn = tg_config.get_bbs_db()
        try:
            conn.execute(
                "INSERT INTO subscription_events (user_id, source, action)"
                " VALUES (1, 'bongnews', 'subscribe')"
            )
            conn.commit()
            stored = conn.execute("SELECT ts FROM subscription_events").fetchone()[0]
        finally:
            conn.close()
        cutoff = cs._events_cutoff(30)
        assert len(cutoff) == len(stored)
        assert cutoff[10] == stored[10] == " "

    def test_an_event_later_on_the_cutoff_date_stays_in_the_window(self):
        cutoff = cs._events_cutoff(1)
        same_day_later = cutoff[:10] + " 23:59:59"
        assert same_day_later > cutoff

        # The old isoformat() cutoff put a "T" where the stored value has a
        # space, and "T" sorts above " ", so this same event fell out entirely.
        iso_cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        assert not same_day_later > iso_cutoff
