"""Tests for the ALLOWED_USER_ID auth gate in handle()."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

    def test_chatid_answers_anyone_with_own_ids_only(self, monkeypatch):
        hit, update = _route(monkeypatch, "!chatid", user_id=424242)
        assert hit == []
        assert len(update.message.replies) == 1
        # Echoes the requester's own ids, never the admin's.
        assert "424242" in update.message.replies[0]
        assert str(ALLOWED_USER_ID) not in update.message.replies[0]
