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
