"""Bridge operations: temp files, token redaction, restart and reboot commands.

Groups the operational fixes: media downloads escaping their directory
(N6 / N18 / R17), the token redaction that keeps a live bot token out of the
log in the first place (N1 follow-up), and the restart and reboot command shape.
"""

import asyncio
import io
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import cmd_discord as cd
import telegram_bridge as tb
import tg_config
from tg_config import ALLOWED_USER_ID


# =========================================================
# N6 / N18 / R17 - media download cannot escape its directory
# =========================================================


class TestSafeMediaFilename:
    @pytest.mark.parametrize(
        "raw",
        [
            "..\\..\\x.txt",
            "../../x.txt",
            "C:\\Windows\\System32\\x.txt",
            "/etc/x.txt",
            "sub/dir/x.txt",
        ],
    )
    def test_path_components_are_stripped(self, raw):
        name = tb._safe_media_filename(raw)
        assert name == "x.txt"


class TestOrphanTempCleanup:
    def test_stale_temp_dirs_are_removed_and_fresh_ones_kept(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(tb, "PROJECTS", {})

        stale = tmp_path / f"{tb._TEMP_PREFIX}old"
        stale.mkdir()
        (stale / "f.bin").write_bytes(b"x")
        old = time.time() - 7200
        os.utime(stale, (old, old))

        fresh = tmp_path / f"{tb._TEMP_PREFIX}new"
        fresh.mkdir()

        unrelated = tmp_path / "someone-elses-dir"
        unrelated.mkdir()
        os.utime(unrelated, (old, old))

        tb._cleanup_orphan_temp()

        assert not stale.exists()
        assert fresh.exists()
        assert unrelated.exists()


class _FakeTgFile:
    def __init__(self):
        self.written = []

    async def download_to_drive(self, path):
        self.written.append(path)
        with open(path, "wb") as f:
            f.write(b"payload")


class _FakeMediaMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **_kw):
        self.replies.append(text)


class _FakeMedia:
    file_id = "fid"

    def __init__(self, file_name=None):
        if file_name is not None:
            self.file_name = file_name


class TestMediaDownload:
    """!news2 downloads a caller-named file, so the path must stay inside the
    temp dir (N6 / N18 / R17)."""

    def _run(self, monkeypatch, tmp_path, media):
        tg_file = _FakeTgFile()
        seen = {}

        class _Bot:
            async def get_file(self, file_id):
                return tg_file

        context = type("C", (), {"bot": _Bot()})()
        update = type("U", (), {"message": _FakeMediaMessage()})()

        monkeypatch.setattr(tb.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "dl"))
        (tmp_path / "dl").mkdir()

        async def _broadcast(upd, content, file_bytes=None, filename=None):
            seen["content"] = content
            seen["bytes"] = file_bytes
            seen["filename"] = filename

        monkeypatch.setattr(tb, "_broadcast_bbnews", _broadcast)
        asyncio.run(tb._handle_news_media(update, context, "!news2 hello", media))
        return update, tg_file, seen

    def test_download_stays_inside_the_temp_dir(self, monkeypatch, tmp_path):
        media = _FakeMedia(file_name="..\\..\\escape.txt")
        _u, tg_file, seen = self._run(monkeypatch, tmp_path, media)
        written = tb.Path(tg_file.written[0]).resolve()
        assert written.parent == (tmp_path / "dl").resolve()
        assert seen["filename"] == "escape.txt"
        assert seen["bytes"] == b"payload"


# =========================================================
# N1 follow-up - bot tokens never reach a log file
# =========================================================


class TestTokenRedaction:
    def _capture(self, emit):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        tg_config.install_token_redaction(handler)
        logger = logging.getLogger("redaction-probe")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        emit(logger)
        return buf.getvalue()

    def test_token_in_a_request_url_is_masked(self):
        out = self._capture(
            lambda lg: lg.info(
                "HTTP Request: POST https://api.telegram.org/bot8123456:AAF-x_Yz9Q/getUpdates"
            )
        )
        assert "AAF-x_Yz9Q" not in out
        assert "bot<redacted>" in out

    def test_token_inside_a_traceback_is_masked(self):
        def _emit(lg):
            try:
                raise RuntimeError("connect to /bot777:ZZ_top-9/sendMessage failed")
            except RuntimeError as e:
                lg.error("request failed", exc_info=e)

        out = self._capture(_emit)
        assert "ZZ_top-9" not in out
        assert "bot<redacted>" in out

    def test_token_passed_as_a_lazy_arg_is_masked(self):
        out = self._capture(lambda lg: lg.warning("url=%s", "https://x/bot42:AbC-dEf_1/x"))
        assert "AbC-dEf_1" not in out


# =========================================================
# N8 / R21 - restart targets exactly one project
# =========================================================


class TestRestartCommandShape:
    def test_root_travels_in_the_environment_not_the_script(self, monkeypatch):
        captured = {}

        def _fake_run(fn, argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            return type("R", (), {"stdout": "1234,started", "stderr": ""})()

        async def _to_thread(fn, *a, **kw):
            return _fake_run(fn, *a, **kw)

        async def _no_sleep(_s):
            return None

        monkeypatch.setattr(cd.asyncio, "to_thread", _to_thread)
        # R8: patch cmd_discord's own seam, not asyncio.sleep, which would be
        # replaced for every coroutine running in this interpreter.
        monkeypatch.setattr(cd, "_sleep", _no_sleep)
        monkeypatch.setattr(cd._cfg, "PROJECTS", {"discord": r"C:\DEV\BBS"})

        replies = []

        class _Msg:
            text = "!re"

            async def reply_text(self, t, **_kw):
                replies.append(t)

        update = type(
            "U",
            (),
            {"message": _Msg(), "effective_user": type("X", (), {"id": ALLOWED_USER_ID})()},
        )()
        asyncio.run(cd.cmd_drestart(update, None))

        argv = captured["argv"]
        ps = argv[-1]
        assert "-NoProfile" in argv
        # N8: nothing from the configured path is interpolated into the script.
        assert r"C:\DEV\BBS" not in ps
        assert captured["env"]["BBS_RESTART_ROOT"] == r"C:\DEV\BBS"
        # R21: the filter is anchored to the root plus a separator, so a sibling
        # checkout such as C:\DEV\BBS-SCV can never match.
        assert "$rootPrefix" in ps
        assert "'*' + $likePrefix + 'run.bat*'" in ps
        # R7: [ and ] are -like wildcards; an unescaped path holding one would
        # match outside the intended directory.
        assert "[WildcardPattern]::Escape($rootPrefix)" in ps
        assert "$rootPrefix + 'run.bat*'" not in ps
        # S8: no root means every -like pattern collapses to '**'.
        assert "if (-not $root)" in ps
        # S8: cmd.exe splits an unquoted path on its first space.
        assert "(" + chr(39) + chr(34) + chr(39) + " + $rootPrefix" in ps


# =========================================================
# !reboot - host reboot needs an explicit "ok", and "cc" aborts it
# =========================================================


class TestRebootCommand:
    def _run(self, monkeypatch, text, returncode=0, raise_exc=None):
        import cmd_moderation as cm
        from tests.fakes import FakeUpdate

        calls = []

        async def _to_thread(fn, argv, **kwargs):
            calls.append((argv, kwargs))
            if raise_exc is not None:
                raise raise_exc
            return type("R", (), {"returncode": returncode, "stdout": "", "stderr": "boom"})()

        monkeypatch.setattr(cm.asyncio, "to_thread", _to_thread)
        update = FakeUpdate(text)
        asyncio.run(cm.cmd_reboot(update, None))
        return calls, update.message.replies

    def test_bare_command_only_prints_usage(self, monkeypatch):
        calls, replies = self._run(monkeypatch, "!reboot")
        assert calls == []
        assert replies and "!reboot ok" in replies[0]

    def test_wrong_argument_does_not_reboot(self, monkeypatch):
        for text in ("!reboot yes", "!rebot ok", "!rebootok"):
            calls, _ = self._run(monkeypatch, text)
            assert calls == [], text
