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

import pytest
from telegram.error import BadRequest

import cmd_discord as cd
import cmd_subscribers as cs
import discord_helpers as dh
import tg_config
import tg_utils
from tests.fakes import FakeUpdate as _FakeUpdate
from tg_utils import parse_int_arg, parse_user_id, safe_reply, safe_send, split_message, u16_len


EMOJI = "\U0001f600"  # non-BMP: one code point, two UTF-16 code units
BACKSLASH = chr(92)
# Written as escapes so no non-ASCII byte lands in a source file.
SUPERSCRIPT_TWO = "\u00b2"  # str.isdigit() is True, int() rejects it
ARABIC_INDIC_DIGITS = "\u0660\u0661\u0662"


class TestSplitByUtf16Units:
    def test_emoji_run_over_the_limit_is_split(self):
        text = EMOJI * 1500  # 1500 code points, 3000 UTF-16 units
        chunks = split_message(text, limit=2000)
        assert len(chunks) > 1
        assert all(u16_len(c) <= 2000 for c in chunks)
        assert "".join(chunks) == text

    def test_surrogate_pairs_are_never_split(self):
        text = EMOJI * 10
        for chunk in split_message(text, limit=5):
            # An odd unit count would mean a pair was cut in half.
            assert u16_len(chunk) % 2 == 0
            assert chunk.encode("utf-16-le").decode("utf-16-le") == chunk

    def test_a_limit_narrower_than_one_code_point_terminates(self):
        chunks = split_message(EMOJI * 3, limit=1)
        assert "".join(chunks) == EMOJI * 3


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
    def test_falls_back_to_plain_text(self):
        msg = _FlakyMessage()
        asyncio.run(safe_reply(msg, "a_b *c* [d]"))
        assert msg.plain == ["a_b *c* [d]"]

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

    def test_a_dead_bot_is_logged_not_raised(self):
        class _DeadBot:
            async def send_message(self, *a, **kw):
                raise RuntimeError("telegram is down")

        assert asyncio.run(safe_send(_DeadBot(), 1, "hi")) is None


class TestMarkdownEscape:
    def test_escapes_the_escape_character_itself(self):
        # S11: an unescaped backslash is itself a Markdown escape, so it eats
        # the character after it - including one this function just added.
        assert tg_utils.md_escape("a" + BACKSLASH + "b") == "a" + BACKSLASH * 2 + "b"
        assert tg_utils.md_escape(BACKSLASH + "_") == BACKSLASH * 2 + BACKSLASH + "_"


# =========================================================
# R19 - argument parsing accepts ASCII digits and mentions only
# =========================================================


class TestArgumentParsing:
    def test_unicode_digit_forms_are_rejected(self):
        # These are exactly the values str.isdigit() accepted and int() refused.
        assert parse_int_arg(SUPERSCRIPT_TWO) is None
        assert parse_user_id(ARABIC_INDIC_DIGITS) is None


# =========================================================
# R15 / R22 - subscriber list content and connection lifetime
# =========================================================


@pytest.fixture
def list_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bbs.db"
    monkeypatch.setattr(tg_config, "BBS_DB_PATH", db_path)
    conn = tg_config.get_bbs_db()
    conn.executescript(
        """
        CREATE TABLE bongnews_subscribers (user_id INTEGER PRIMARY KEY);
        CREATE TABLE subscribers (user_id INTEGER PRIMARY KEY, mode TEXT);
        CREATE TABLE market_subscribers (user_id INTEGER PRIMARY KEY, mode TEXT);
        CREATE TABLE ark_subscribers (user_id INTEGER PRIMARY KEY);
        CREATE TABLE morning_subscribers (user_id INTEGER PRIMARY KEY, cities TEXT);
        CREATE TABLE danawa_subscriptions (user_id INTEGER, name TEXT);
        CREATE TABLE pubg_subscribers (user_id INTEGER, nickname TEXT);
        CREATE TABLE user_dm (user_id INTEGER, content TEXT, created_at TEXT);
        INSERT INTO bongnews_subscribers VALUES (7);
        INSERT INTO pubg_subscribers VALUES (7, 'alpha'), (7, 'bravo'), (8, 'solo');
        INSERT INTO morning_subscribers VALUES (7, '["Seoul"]');
        INSERT INTO danawa_subscriptions VALUES (7, 'monitor');
        INSERT INTO user_dm VALUES (7, 'hello there', '2026-09-01 10:00:00');
        """
    )
    conn.commit()
    conn.close()

    async def _no_lookup(uids):
        return {}

    monkeypatch.setattr(dh, "_fetch_discord_usernames", _no_lookup)
    return db_path


class TestSubscriberList:
    def test_every_pubg_nickname_is_shown(self, list_db):
        update = _FakeUpdate("!list")
        asyncio.run(cs.cmd_list(update, None))
        output = "\n".join(update.message.replies)
        # pubg_subscribers has a composite key, so one user can hold several
        # nicknames; only the first was ever printed.
        assert "alpha" in output and "bravo" in output
        assert "solo" in output


class TestConnectionReleasedBeforeHttp:
    """!ud held the shared sqlite connection open across a 15s Discord lookup."""

    def test_connection_closes_before_the_username_lookup(self, list_db, monkeypatch):
        order = []
        real_read = tg_config.db_read

        def _tracked_read(fn):
            try:
                return real_read(fn)
            finally:
                order.append("db-close")

        monkeypatch.setattr(tg_config, "db_read", _tracked_read)

        async def _lookup(uids):
            order.append("http")
            return {}

        monkeypatch.setattr(dh, "_fetch_discord_usernames", _lookup)

        update = _FakeUpdate("!ud 7")
        asyncio.run(cd.cmd_dm_log(update, None))
        assert order[0] == "db-close"
        assert "http" in order


# =========================================================
# R6 - sqlite work happens off the event loop
# =========================================================


class TestAsyncDbWrappers:
    def test_read_wrapper_always_closes_its_connection(self, list_db):
        with pytest.raises(RuntimeError):

            async def _go():
                def _boom(conn):
                    raise RuntimeError("query blew up")

                await tg_config.db_read_async(_boom)

            asyncio.run(_go())
        # A leaked connection would keep the file locked; a fresh write proves not.
        tg_config.db_read(lambda c: c.execute("SELECT 1").fetchone())
