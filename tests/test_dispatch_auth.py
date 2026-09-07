"""Tests for the highest-risk untested paths: the ALLOWED_USER_ID auth gate in
handle(), and _dispatch_text routing (exact-token match plus the attached-
argument prefix fallback), plus parse_schedule_time's branches.

These pin the exact behaviors AGENTS.md's debugging playbook calls out as
scenario P1 (prefix collision: !re vs !re0)."""

import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

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

    def test_command_with_args_routes_by_first_token(self, monkeypatch):
        hit, _ = _route(monkeypatch, "!dm 123456789012345678 hello")
        assert hit == ["!dm"]

    def test_attached_argument_falls_back_to_prefix(self, monkeypatch):
        # "!log1" has no space, so exact-token lookup misses and the ordered
        # prefix fallback must still resolve it.
        hit, _ = _route(monkeypatch, "!log1")
        assert hit == ["!log"]

    def test_unknown_command_gets_help_pointer(self, monkeypatch):
        hit, update = _route(monkeypatch, "!nope")
        assert hit == []
        assert update.message.replies and "!help" in update.message.replies[0]


class TestParseScheduleTime:
    """parse_schedule_time is the highest-complexity pure function in the repo
    and had zero coverage. One test per branch + rollover + validation."""

    def test_no_time_prefix_raises_no_time(self):
        with pytest.raises(ValueError) as e:
            cns.parse_schedule_time("그냥 내용만 있는 뉴스")
        assert str(e.value) == cns._NO_TIME

    def test_tomorrow_prefix(self):
        target, content = cns.parse_schedule_time("내일 08:30 아침 뉴스")
        kst = target.astimezone(cns.KST)
        tomorrow = (datetime.now(cns.KST) + timedelta(days=1)).date()
        assert (kst.date(), kst.hour, kst.minute) == (tomorrow, 8, 30)
        assert content == "아침 뉴스"

    def test_time_then_tomorrow_variant(self):
        target, content = cns.parse_schedule_time("08:30 내일 아침 뉴스")
        kst = target.astimezone(cns.KST)
        tomorrow = (datetime.now(cns.KST) + timedelta(days=1)).date()
        assert (kst.date(), kst.hour, kst.minute) == (tomorrow, 8, 30)
        assert content == "아침 뉴스"

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

    def test_month_day_in_the_future_stays_this_year(self):
        now = datetime.now(cns.KST)
        future = now + timedelta(days=30)
        raw = f"{future.month}/{future.day} 09:00 공지"
        target, _ = cns.parse_schedule_time(raw)
        assert target.astimezone(cns.KST).year == future.year

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
