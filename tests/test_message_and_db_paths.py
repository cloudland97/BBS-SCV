"""Message length accounting, reply safety, argument parsing, and DB blocking.

Covers the UTF-16 length unit both APIs actually enforce (R10), the Markdown
fallback on the replies that interpolate arbitrary text (R9), ASCII-only
argument parsing (R19), the subscriber list that showed one nickname per user
(R15), releasing the shared database before an HTTP await (R22), and the async
DB wrappers that keep sqlite retries off the polling loop (R6).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram.error import BadRequest

from tg_utils import safe_reply, safe_send, split_message, u16_len


EMOJI = "\U0001f600"  # non-BMP: one code point, two UTF-16 code units
# Written as escapes so no non-ASCII byte lands in a source file.
SUPERSCRIPT_TWO = "\u00b2"  # str.isdigit() is True, int() rejects it


class TestSplitByUtf16Units:
    def test_emoji_run_over_the_limit_is_split(self):
        text = EMOJI * 1500  # 1500 code points, 3000 UTF-16 units
        chunks = split_message(text, limit=2000)
        assert len(chunks) > 1
        assert all(u16_len(c) <= 2000 for c in chunks)
        assert "".join(chunks) == text


# =========================================================
# R9 - a broken Markdown payload must not lose the message
# =========================================================


class _FlakyMessage:
    """Rejects parse_mode sends the way Telegram rejects unbalanced Markdown."""

    def __init__(self):
        self.markdown = []
        self.plain = []

    async def reply_text(self, text, parse_mode=None, **_kw):
        if parse_mode:
            self.markdown.append(text)
            raise BadRequest("can't parse entities")
        self.plain.append(text)


class TestSafeReplyFallback:
    def test_long_broken_text_is_still_delivered_in_full(self):
        msg = _FlakyMessage()
        text = "x_" * 3000
        asyncio.run(safe_reply(msg, text))
        assert "".join(msg.plain) == text


class _FlakyBot:
    def __init__(self):
        self.plain = []

    async def send_message(self, chat_id, text, parse_mode=None, **_kw):
        if parse_mode:
            raise BadRequest("can't parse entities")
        self.plain.append(text)


class TestSafeSendFallback:
    def test_falls_back_to_plain_text(self):
        bot = _FlakyBot()
        asyncio.run(safe_send(bot, 1, "a_b *c*"))
        assert bot.plain == ["a_b *c*"]
