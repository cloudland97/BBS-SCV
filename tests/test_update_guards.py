"""Guards for updates that carry no message and no user.

PTB message filters key off effective_message, so edited_message, channel_post,
edited_channel_post and business_message all reach the bridge's MessageHandler.
Both entry points used to dereference update.message / update.effective_user
before the permission gate, so merely editing a message at the bot produced an
AttributeError traceback instead of silence (N2 / N3 / R5).

test_dispatch_auth's fakes always set message and effective_user, which is why
neither crash was reachable from the existing suite.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_bridge as tb
import tg_utils
from tests.fakes import FakeMessage as _FakeMessage
from tg_config import ALLOWED_USER_ID


class _NonMessageUpdate:
    """An update whose payload is not a plain message.

    edited_message / channel_post live in their own attributes; update.message
    is None for every one of them.
    """

    def __init__(self, kind, user=None, text="!help"):
        self.message = None
        setattr(self, kind, _FakeMessage(text))
        self.effective_user = user
        self.effective_chat = None


class TestHandleWithoutMessage:
    def test_edited_message_is_ignored(self):
        update = _NonMessageUpdate("edited_message", user=type("U", (), {"id": ALLOWED_USER_ID})())
        # Must return quietly; before the guard this raised AttributeError.
        assert asyncio.run(tb.handle(update, None)) is None
        assert update.edited_message.replies == []

    def test_channel_post_is_ignored(self):
        update = _NonMessageUpdate("channel_post", user=None)
        assert asyncio.run(tb.handle(update, None)) is None
        assert update.channel_post.replies == []

    def test_business_message_is_ignored(self):
        update = _NonMessageUpdate("business_message", user=None, text="!re0")
        assert asyncio.run(tb.handle(update, None)) is None


class TestHandleWithoutUser:
    """A message with no effective_user must not pass the gate.

    An anonymous/channel-attributed post carries sender_chat instead of a user;
    `update.effective_user.id` raised before the comparison could reject it.
    """

    def test_message_without_user_is_rejected(self):
        update = _NonMessageUpdate("channel_post")
        update.message = _FakeMessage("!re0")
        asyncio.run(tb.handle(update, None))
        assert update.message.replies == ["No permission"]

    def test_chatid_without_user_does_not_crash(self):
        update = _NonMessageUpdate("channel_post")
        update.message = _FakeMessage("!chatid")
        asyncio.run(tb.handle(update, None))
        assert len(update.message.replies) == 1
        assert "user_id" in update.message.replies[0]


class TestAdminOnlyWithoutUser:
    def _run(self, user):
        calls = []

        @tg_utils.admin_only
        async def _handler(update, context):
            calls.append(1)

        update = type("U", (), {"effective_user": user})()
        asyncio.run(_handler(update, None))
        return calls

    def test_none_user_is_rejected_not_crashed(self):
        assert self._run(None) == []

    def test_wrong_user_is_rejected(self):
        assert self._run(type("U", (), {"id": ALLOWED_USER_ID + 1})()) == []

    def test_admin_still_passes(self):
        assert self._run(type("U", (), {"id": ALLOWED_USER_ID})()) == [1]
