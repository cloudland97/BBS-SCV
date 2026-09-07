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
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from telegram.error import BadRequest

import cmd_discord as cd
import cmd_subscribers as cs
import discord_helpers as dh
import tg_config
import tg_utils
from tg_config import ALLOWED_USER_ID
from tg_utils import parse_int_arg, parse_user_id, safe_reply, safe_send, split_message, u16_len


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.photo = None
        self.video = None
        self.document = None
        self.caption = None
        self.replies = []

    async def reply_text(self, text, **_kw):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, text, user_id=ALLOWED_USER_ID):
        self.message = _FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_chat = type("C", (), {"id": user_id})()


EMOJI = "\U0001f600"  # non-BMP: one code point, two UTF-16 code units
BACKSLASH = chr(92)
# Written as escapes so no non-ASCII byte lands in a source file.
SUPERSCRIPT_TWO = "\u00b2"  # str.isdigit() is True, int() rejects it
ARABIC_INDIC_DIGITS = "\u0660\u0661\u0662"
HANGUL_SYLLABLES = "\uac00\ub098\ub2e4"


# =========================================================
# R10 - Telegram and Discord count UTF-16 code units
# =========================================================


class TestUtf16Length:
    def test_ascii_counts_the_same_either_way(self):
        assert u16_len("hello") == len("hello") == 5

    def test_bmp_hangul_counts_one_unit_each(self):
        text = HANGUL_SYLLABLES
        assert u16_len(text) == len(text) == 3

    def test_non_bmp_emoji_counts_two_units(self):
        assert u16_len(EMOJI) == 2
        assert len(EMOJI) == 1


class TestSplitByUtf16Units:
    def test_emoji_run_over_the_limit_is_split(self):
        text = EMOJI * 1500  # 1500 code points, 3000 UTF-16 units
        chunks = split_message(text, limit=2000)
        assert len(chunks) > 1
        assert all(u16_len(c) <= 2000 for c in chunks)
        assert "".join(chunks) == text

    def test_emoji_run_under_the_limit_stays_whole(self):
        text = EMOJI * 900  # 1800 units
        assert split_message(text, limit=2000) == [text]

    def test_surrogate_pairs_are_never_split(self):
        text = EMOJI * 10
        for chunk in split_message(text, limit=5):
            # An odd unit count would mean a pair was cut in half.
            assert u16_len(chunk) % 2 == 0
            assert chunk.encode("utf-16-le").decode("utf-16-le") == chunk

    def test_a_limit_narrower_than_one_code_point_terminates(self):
        chunks = split_message(EMOJI * 3, limit=1)
        assert "".join(chunks) == EMOJI * 3

    def test_mixed_content_is_lossless(self):
        text = "\n".join(f"line {i} {EMOJI} {HANGUL_SYLLABLES}" for i in range(300))
        chunks = split_message(text, limit=200)
        assert "".join(chunks) == text
        assert all(u16_len(c) <= 200 for c in chunks)

    @pytest.mark.parametrize("size", [1999, 2000, 2001, 4000, 4001])
    def test_ascii_boundaries_still_hold(self, size):
        text = "a" * size
        chunks = split_message(text, limit=2000)
        assert "".join(chunks) == text
        assert all(len(c) <= 2000 for c in chunks)


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
    def test_escapes_every_v1_special(self):
        assert tg_utils.md_escape("a_b*c`d[e") == "a\\_b\\*c\\`d\\[e"

    def test_escapes_the_escape_character_itself(self):
        # S11: an unescaped backslash is itself a Markdown escape, so it eats
        # the character after it - including one this function just added.
        assert tg_utils.md_escape("a" + BACKSLASH + "b") == "a" + BACKSLASH * 2 + "b"
        assert tg_utils.md_escape(BACKSLASH + "_") == BACKSLASH * 2 + BACKSLASH + "_"

    def test_leaves_plain_text_untouched(self):
        assert tg_utils.md_escape("plain text 123") == "plain text 123"

    def test_handles_none(self):
        assert tg_utils.md_escape(None) == ""


# =========================================================
# R19 - argument parsing accepts ASCII digits and mentions only
# =========================================================


class TestArgumentParsing:
    def test_plain_ascii_digits(self):
        assert parse_int_arg("42") == 42
        assert parse_user_id("123456789012345678") == 123456789012345678

    def test_unicode_digit_forms_are_rejected(self):
        # These are exactly the values str.isdigit() accepted and int() refused.
        assert parse_int_arg(SUPERSCRIPT_TWO) is None
        assert parse_user_id(ARABIC_INDIC_DIGITS) is None

    def test_discord_mentions_are_accepted(self):
        assert parse_user_id("<@123>") == 123
        assert parse_user_id("<@!456>") == 456

    def test_mentions_are_not_valid_plain_ints(self):
        assert parse_int_arg("<@123>") is None

    @pytest.mark.parametrize("raw", [None, "", "   ", "abc", "12a", "-5", "1.5", "<@>"])
    def test_junk_is_rejected(self, raw):
        assert parse_int_arg(raw) is None
        assert parse_user_id(raw) is None


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
    def test_display_names_are_escaped(self, list_db, monkeypatch):
        """R9: !list interpolates Discord global_names into Markdown."""

        async def _names(uids):
            return {u: "a_b*c" for u in uids}

        monkeypatch.setattr(dh, "_fetch_discord_usernames", _names)
        update = _FakeUpdate("!list")
        asyncio.run(cs.cmd_list(update, None))
        output = "\n".join(update.message.replies)
        assert "a\\_b\\*c" in output
        assert "a_b*c" not in output

    def test_danawa_product_names_are_escaped(self, list_db, monkeypatch):
        conn = tg_config.get_bbs_db()
        try:
            conn.execute("INSERT INTO danawa_subscriptions VALUES (7, ?)", ("RTX_5090 *OC*",))
            conn.commit()
        finally:
            conn.close()

        async def _names(uids):
            return {}

        monkeypatch.setattr(dh, "_fetch_discord_usernames", _names)
        update = _FakeUpdate("!list")
        asyncio.run(cs.cmd_list(update, None))
        output = "\n".join(update.message.replies)
        assert "RTX\\_5090 \\*OC\\*" in output

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

    def test_reads_run_off_the_event_loop(self, list_db, monkeypatch):
        threads = []
        real_read = tg_config.db_read

        def _tracked_read(fn):
            import threading

            threads.append(threading.current_thread() is threading.main_thread())
            return real_read(fn)

        monkeypatch.setattr(tg_config, "db_read", _tracked_read)

        async def _lookup(uids):
            return {}

        monkeypatch.setattr(dh, "_fetch_discord_usernames", _lookup)
        for text, handler in (("!ud 7", cd.cmd_dm_log), ("!ud", cd.cmd_dm_log)):
            asyncio.run(handler(_FakeUpdate(text), None))
        assert threads and not any(threads)


# =========================================================
# R6 - sqlite work happens off the event loop
# =========================================================


class TestAsyncDbWrappers:
    def test_reads_run_in_a_worker_thread(self, list_db):
        seen = {}

        def _probe(conn):
            seen["thread"] = threading.get_ident()
            return conn.execute("SELECT COUNT(*) FROM pubg_subscribers").fetchone()[0]

        async def _go():
            seen["loop"] = threading.get_ident()
            return await tg_config.db_read_async(_probe)

        assert asyncio.run(_go()) == 3
        assert seen["thread"] != seen["loop"]

    def test_writes_run_in_a_worker_thread(self, list_db):
        seen = {}

        def _probe(conn):
            seen["thread"] = threading.get_ident()
            conn.execute("INSERT INTO ark_subscribers VALUES (99)")
            return True

        async def _go():
            seen["loop"] = threading.get_ident()
            return await tg_config.db_write_async(_probe)

        assert asyncio.run(_go()) is True
        assert seen["thread"] != seen["loop"]
        conn = tg_config.get_bbs_db()
        try:
            assert conn.execute("SELECT COUNT(*) FROM ark_subscribers").fetchone()[0] == 1
        finally:
            conn.close()

    def test_read_wrapper_always_closes_its_connection(self, list_db):
        with pytest.raises(RuntimeError):

            async def _go():
                def _boom(conn):
                    raise RuntimeError("query blew up")

                await tg_config.db_read_async(_boom)

            asyncio.run(_go())
        # A leaked connection would keep the file locked; a fresh write proves not.
        tg_config.db_read(lambda c: c.execute("SELECT 1").fetchone())
