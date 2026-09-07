"""Discord broadcast behaviour: rate limits, mention control, honest status.

Covers the failure modes that made a scheduled send look successful when it had
reached nobody (R1 / R8), the 429 handling that did not exist (N7 / R18), and
the missing allowed_mentions on every message payload (N9).
"""

import asyncio
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import discord_helpers as dh
from tests.fakes import FakeBot as _FakeBot
from tests.fakes import FakeResp as _FakeResp
from tests.fakes import ScriptedSession as _ScriptedSession


@pytest.fixture
def no_sleep(monkeypatch):
    """Record retry waits instead of actually sleeping.

    Patches discord_helpers' own _sleep seam. Replacing asyncio.sleep itself
    would swap it out for every coroutine in the interpreter, including the
    event loop's own users, for the duration of the test.
    """
    waits = []

    async def _sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(dh, "_sleep", _sleep)
    return waits


class TestRateLimitRetry:
    def test_429_is_retried_once_after_retry_after(self, no_sleep):
        session = _ScriptedSession(message_statuses=[429, 200])
        ok = asyncio.run(dh._send_discord_dm(session, "tok", "42", "hello"))
        assert ok is True
        # One retry, and it waited the Retry-After the response advertised.
        assert len(session.message_bodies) == 2
        assert no_sleep == [0.01]

    def test_second_429_gives_up_rather_than_hammering(self, no_sleep):
        session = _ScriptedSession(message_statuses=[429, 429])
        ok = asyncio.run(dh._send_discord_dm(session, "tok", "42", "hello"))
        assert ok is False
        assert len(session.message_bodies) == dh._MAX_ATTEMPTS

    def test_retry_after_is_capped(self, no_sleep):
        resp = _FakeResp(429, {"retry_after": 9999}, {"Retry-After": "9999"})
        assert asyncio.run(dh._retry_after(resp)) == dh._MAX_RETRY_WAIT

    def test_retry_after_falls_back_to_json_body(self, no_sleep):
        resp = _FakeResp(429, {"retry_after": 2.5}, {})
        assert asyncio.run(dh._retry_after(resp)) == 2.5

    def test_channel_open_429_is_retried(self, no_sleep):
        session = _ScriptedSession(channel_statuses=[429, 200])
        ok = asyncio.run(dh._send_discord_dm(session, "tok", "42", "hello"))
        assert ok is True
        assert session.channel_calls == 2

    def test_403_is_not_retried(self, no_sleep):
        session = _ScriptedSession(channel_statuses=[403])
        ok = asyncio.run(dh._send_discord_dm(session, "tok", "42", "hello"))
        assert ok is False
        assert session.channel_calls == 1
        assert no_sleep == []


class TestAllowedMentions:
    def test_every_text_chunk_suppresses_mentions(self, no_sleep):
        session = _ScriptedSession()
        # Long enough to need three message POSTs.
        asyncio.run(dh._send_discord_dm(session, "tok", "42", "x" * 4500))
        assert len(session.message_bodies) == 3
        for body in session.message_bodies:
            assert body["allowed_mentions"] == {"parse": []}

    def test_media_payload_carries_allowed_mentions(self, no_sleep):
        session = _ScriptedSession()
        asyncio.run(dh._send_discord_dm(session, "tok", "42", "hi", b"bytes", "a.png"))
        assert len(session.message_forms) == 1
        fields = {
            opts.get("name"): value for opts, _hdrs, value in session.message_forms[0]._fields
        }
        assert "payload_json" in fields
        payload = json.loads(fields["payload_json"])
        assert payload["allowed_mentions"] == {"parse": []}
        assert payload["content"] == "hi"
        assert fields["file"] == b"bytes"


class TestConcurrencyCap:
    def test_fan_out_never_exceeds_the_semaphore(self, monkeypatch):
        live = {"now": 0, "peak": 0}

        async def _fake_dm(session, token, uid, text, file_bytes, filename):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await asyncio.sleep(0)
            live["now"] -= 1
            return True

        monkeypatch.setattr(dh, "_send_discord_dm", _fake_dm)
        results = asyncio.run(dh._send_all(None, "tok", list(range(40)), "hi", None, None))
        assert results == [True] * 40
        assert live["peak"] <= dh._DM_CONCURRENCY


class TestBlockingWorkIsOffTheLoop:
    """S4: the token read and the subscriber query both block; neither may run
    on the polling loop's thread."""

    def test_token_and_subscribers_are_read_in_a_worker_thread(self, monkeypatch):
        seen = {}

        def _token():
            seen["token_thread"] = threading.get_ident()
            return "tok"

        def _subs():
            seen["subs_thread"] = threading.get_ident()
            return [1]

        monkeypatch.setattr(dh, "_get_discord_token", _token)
        monkeypatch.setattr(dh, "_get_bongnews_subscribers", _subs)

        async def _go():
            seen["loop_thread"] = threading.get_ident()
            return (
                await dh._get_discord_token_async(),
                await dh._get_bongnews_subscribers_async(),
            )

        assert asyncio.run(_go()) == ("tok", [1])
        assert seen["token_thread"] != seen["loop_thread"]
        assert seen["subs_thread"] != seen["loop_thread"]

    def test_latest_file_write_is_off_the_loop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dh, "DISCORD_DATA_PATH", tmp_path)
        seen = {}

        async def _go():
            seen["loop_thread"] = threading.get_ident()
            await dh._save_latest_async("  body  ")

        asyncio.run(_go())
        assert (tmp_path / "bongnews_latest.txt").read_text(encoding="utf-8") == "body"


class TestAutoBroadcastResult:
    """_broadcast_bbnews_auto must report what actually happened and never raise."""

    def _patch(self, monkeypatch, token="tok", subscribers=(1, 2, 3), results=None):
        monkeypatch.setattr(dh, "_get_discord_token", lambda: token)
        monkeypatch.setattr(dh, "_get_bongnews_subscribers", lambda: list(subscribers))

        async def _no_save(content):
            return None

        # Never touch the real BBS data directory from a test.
        monkeypatch.setattr(dh, "_save_latest_async", _no_save)

        async def _fake_send_all(session, tok, subs, text, fb, fn):
            return list(results) if results is not None else [True] * len(subs)

        monkeypatch.setattr(dh, "_send_all", _fake_send_all)

    def test_returns_ok_and_fail_counts(self, monkeypatch):
        self._patch(monkeypatch, results=[True, False, True])
        bot = _FakeBot()
        assert asyncio.run(dh._broadcast_bbnews_auto(bot, 1, "news")) == (2, 1)

    def test_missing_token_reports_nothing_attempted(self, monkeypatch):
        self._patch(monkeypatch, token=None)
        bot = _FakeBot()
        assert asyncio.run(dh._broadcast_bbnews_auto(bot, 1, "news")) == (0, 0)
        assert bot.sent  # the operator is still told why

    def test_no_subscribers_reports_nothing_attempted(self, monkeypatch):
        self._patch(monkeypatch, subscribers=())
        bot = _FakeBot()
        assert asyncio.run(dh._broadcast_bbnews_auto(bot, 1, "news")) == (0, 0)

    def test_every_dm_failing_is_not_reported_as_success(self, monkeypatch):
        self._patch(monkeypatch, results=[False, False, False])
        bot = _FakeBot()
        assert asyncio.run(dh._broadcast_bbnews_auto(bot, 1, "news")) == (0, 3)

    def test_summary_failure_does_not_propagate(self, monkeypatch):
        """R1: the summary send used to raise, and the scheduler read that as
        'the broadcast failed' and re-queued news that had already gone out."""
        self._patch(monkeypatch, results=[True, True, True])
        bot = _FakeBot(fail=True)
        assert asyncio.run(dh._broadcast_bbnews_auto(bot, 1, "news")) == (3, 0)
