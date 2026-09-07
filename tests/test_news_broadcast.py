"""Regression tests for long-news broadcast chunking.

A news item over Discord's 2000-char content limit used to fail the whole
broadcast with a 400. _send_discord_dm now splits via tg_utils.split_message.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord_helpers as dh
from tests.fakes import FakeResp as _FakeResp
from tg_utils import split_message


class _FakeSession:
    """Records every POST; returns 200 for channel-open and message sends."""

    def __init__(self):
        self.message_posts = []

    def post(self, url, json=None, data=None, headers=None):
        if url.endswith("/users/@me/channels"):
            return _FakeResp(200, {"id": "chan1"})
        # a message send (json content or multipart form)
        self.message_posts.append(json["content"] if json else data)
        return _FakeResp(200, {})


class TestSplitForDiscord:
    def test_long_news_splits_under_limit(self):
        text = "**bongnews**\n\n" + "\n\n".join("가" * 180 for _ in range(12))
        assert len(text) > 2000
        chunks = split_message(text, limit=2000)
        assert len(chunks) > 1
        assert all(len(c) <= 2000 for c in chunks)
        assert "".join(chunks) == text

    def test_short_news_single_chunk(self):
        assert split_message("hi", limit=2000) == ["hi"]


class TestSendDiscordDm:
    def test_over_limit_text_sends_multiple_messages(self):
        session = _FakeSession()
        text = "x" * 4500  # 3 chunks at 2000
        ok = asyncio.run(dh._send_discord_dm(session, "tok", "42", text))
        assert ok is True
        assert len(session.message_posts) == 3
        assert all(len(m) <= 2000 for m in session.message_posts)
        assert "".join(session.message_posts) == text

    def test_under_limit_text_sends_one_message(self):
        session = _FakeSession()
        ok = asyncio.run(dh._send_discord_dm(session, "tok", "42", "short news"))
        assert ok is True
        assert session.message_posts == ["short news"]
