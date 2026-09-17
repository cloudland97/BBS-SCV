"""Tests for the highest-risk untested paths: the ALLOWED_USER_ID auth gate in
handle(), and _dispatch_text routing (exact-token match plus the attached-
argument prefix fallback), plus parse_schedule_time's branches.

These pin the exact behaviors AGENTS.md's debugging playbook calls out as
scenario P1 (prefix collision: !re vs !re0)."""

import pytest
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cmd_news_schedule as cns
import telegram_bridge as tb
from tests.fakes import FakeUpdate as _FakeUpdate
from tg_config import ALLOWED_USER_ID


def _route(monkeypatch, text, user_id=ALLOWED_USER_ID):
    """Run handle() with every command handler stubbed; return (hit, update)."""
    hit = []

    def _stub(name):
        async def _h(update, context):
            hit.append(name)

        return _h

    table = {cmd: _stub(cmd) for cmd, _ in tb._CMD_ROUTES}
    monkeypatch.setattr(tb, "_CMD_TABLE", table)
    monkeypatch.setattr(tb, "_CMD_ROUTES", [(c, table[c]) for c, _ in tb._CMD_ROUTES])

    update = _FakeUpdate(text, user_id)
    asyncio.run(tb.handle(update, None))
    return hit, update


class TestAuthGate:
    def test_non_admin_is_rejected_before_dispatch(self, monkeypatch):
        hit, update = _route(monkeypatch, "!help", user_id=999999999)
        assert hit == []
        assert update.message.replies == ["No permission"]

    def test_admin_reaches_dispatch(self, monkeypatch):
        hit, _ = _route(monkeypatch, "!help")
        assert hit == ["!help"]

    def test_chatid_answers_anyone_with_own_ids_only(self, monkeypatch):
        hit, update = _route(monkeypatch, "!chatid", user_id=424242)
        assert hit == []
        assert len(update.message.replies) == 1
        # Echoes the requester's own ids, never the admin's.
        assert "424242" in update.message.replies[0]
        assert str(ALLOWED_USER_ID) not in update.message.replies[0]


class TestDispatchRouting:
    def test_re0_not_shadowed_by_re(self, monkeypatch):
        hit, _ = _route(monkeypatch, "!re0")
        assert hit == ["!re0"]

    def test_reboot_not_shadowed_by_re(self, monkeypatch):
        hit, _ = _route(monkeypatch, "!reboot ok")
        assert hit == ["!reboot"]
        # Attached form misses the exact table and must still win the prefix fallback.
        hit, _ = _route(monkeypatch, "!rebootok")
        assert hit == ["!reboot"]

    def test_reboot_typo_never_reaches_discord_restart(self, monkeypatch):
        # A near-miss must land on the reboot usage text, never on "!re".
        for typo in ("!rebot ok", "!reboo ok", "!rebooot"):
            hit, _ = _route(monkeypatch, typo)
            assert hit in (["!reboot"], ["!reb"]), typo


class TestParseScheduleTime:
    """parse_schedule_time is the highest-complexity pure function in the repo
    and had zero coverage. One test per branch + rollover + validation."""

    def test_month_day_in_the_past_rolls_forward_never_backward(self):
        # Invariant form (year-boundary safe): a past M/D must land in the
        # future with the M/D preserved, i.e. the rollover fired.
        now = datetime.now(cns.KST)
        past = now - timedelta(days=30)
        raw = f"{past.month}/{past.day} 09:00 연례 공지"
        target, content = cns.parse_schedule_time(raw)
        kst = target.astimezone(cns.KST)
        assert kst > now
        assert (kst.month, kst.day) == (past.month, past.day)
        assert content == "연례 공지"

    def test_bare_time_already_passed_rolls_to_tomorrow(self):
        # Invariant form (midnight-safe): a just-passed HH:MM must schedule in
        # the future with the HH:MM preserved.
        now = datetime.now(cns.KST)
        past = now - timedelta(minutes=10)
        raw = f"{past.hour:02d}:{past.minute:02d} 지난 시각 뉴스"
        target, _ = cns.parse_schedule_time(raw)
        kst = target.astimezone(cns.KST)
        assert kst > now
        assert (kst.hour, kst.minute) == (past.hour, past.minute)

    def test_invalid_hour_rejected(self):
        with pytest.raises(ValueError) as e:
            cns.parse_schedule_time("내일 25:00 뉴스")
        assert str(e.value) != cns._NO_TIME

    def test_invalid_calendar_date_rejected(self):
        with pytest.raises(ValueError) as e:
            cns.parse_schedule_time("2/30 08:00 뉴스")
        assert str(e.value) != cns._NO_TIME
