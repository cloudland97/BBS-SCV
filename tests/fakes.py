"""Shared fake objects used across multiple test files.

Each class here replaces near-identical per-file copies of Telegram/Discord
stand-ins. Keep these minimal: add a parameter only when an existing call
site actually needs it, rather than anticipating future needs.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tg_config import ALLOWED_USER_ID


# ── python-telegram-bot stand-ins ────────────────────────────────────────────


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.photo = None
        self.video = None
        self.document = None
        self.caption = None
        self.replies = []

    async def reply_text(self, text, **_kw):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, text, user_id=ALLOWED_USER_ID):
        self.message = FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_chat = type("C", (), {"id": user_id})()


class FakeBot:
    """Telegram bot stand-in. fail=True makes send_message raise."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, **_kw):
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append(text)


# ── Discord REST stand-ins ───────────────────────────────────────────────────


class FakeResp:
    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class ScriptedSession:
    """Session that replays a queued status per message POST.

    Channel-open always succeeds unless `channel_statuses` is supplied.
    """

    def __init__(self, message_statuses=None, channel_statuses=None):
        self.message_statuses = list(message_statuses or [])
        self.channel_statuses = list(channel_statuses or [])
        self.message_bodies = []
        self.message_forms = []
        self.channel_calls = 0

    def post(self, url, json=None, data=None, headers=None):
        if url.endswith("/users/@me/channels"):
            self.channel_calls += 1
            status = self.channel_statuses.pop(0) if self.channel_statuses else 200
            if status == 200:
                return FakeResp(200, {"id": "chan1"})
            return FakeResp(status, {"retry_after": 0.01}, {"Retry-After": "0.01"})
        if json is not None:
            self.message_bodies.append(json)
        else:
            self.message_forms.append(data)
        status = self.message_statuses.pop(0) if self.message_statuses else 200
        if status == 429:
            return FakeResp(429, {"retry_after": 0.01}, {"Retry-After": "0.01"})
        return FakeResp(status, {})
