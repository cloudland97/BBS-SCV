"""Bridge operations: log paths, temp files, single instance, maintenance.

Groups the operational fixes: the BBS log filenames !log and the post-restart
check were guessing wrong (N4 / N5), media downloads escaping their directory
(N6 / N18 / R17), no single-instance guard (R4), retention and heartbeat that
only ever ran at boot (N14 / R11), and the token redaction that keeps a live bot
token out of the log in the first place (N1 follow-up).
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
    def __init__(self, fail=False):
        self.fail = fail
        self.written = []

    async def download_to_drive(self, path):
        if self.fail:
            raise RuntimeError("file is too big")
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
    """!news2 downloads a caller-named file, so the path and the failure modes
    both matter (N6 / N18 / R16 / R17)."""

    def _run(self, monkeypatch, tmp_path, media, fail=False):
        tg_file = _FakeTgFile(fail=fail)
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

    def test_a_failed_download_tells_the_operator(self, monkeypatch, tmp_path):
        update, _f, seen = self._run(
            monkeypatch, tmp_path, _FakeMedia(file_name="big.mp4"), fail=True
        )
        # R16: an oversized file used to produce no reply at all.
        assert update.message.replies
        assert update.message.replies[0].startswith("❌")
        assert "content" not in seen  # the broadcast never ran

    def test_get_file_failure_tells_the_operator(self, monkeypatch, tmp_path):
        class _BrokenBot:
            async def get_file(self, file_id):
                raise RuntimeError("Bad Request: file is too big")

        context = type("C", (), {"bot": _BrokenBot()})()
        update = type("U", (), {"message": _FakeMediaMessage()})()
        asyncio.run(tb._handle_news_media(update, context, "!news2 hi", _FakeMedia("a.png")))
        assert update.message.replies
        assert update.message.replies[0].startswith("❌")


# =========================================================
# N14 / R11 - daily retention and heartbeat
# =========================================================


class TestDailyMaintenance:
    def test_startup_registers_all_background_tasks(self, tmp_path, monkeypatch):
        """Asserts the tasks actually exist, not that the source mentions them."""
        monkeypatch.setattr(tb, "LOG_DIR", tmp_path)
        monkeypatch.setattr(tb, "RESTART_MARKER", tmp_path / "restart_pending")
        monkeypatch.setattr(tb, "_cleanup_orphan_temp", lambda *a, **kw: None)
        monkeypatch.setattr(tb, "check_shared_schema", lambda: [])
        monkeypatch.setattr(tb, "BBS_DB_PATH", tmp_path / "absent.db")
        monkeypatch.setattr(tb, "_bg_tasks", [])

        async def _go():
            await tb.post_init(type("App", (), {"bot": None})())
            names = {t.get_coro().__qualname__ for t in tb._bg_tasks}
            for task in tb._bg_tasks:
                task.cancel()
            await asyncio.gather(*tb._bg_tasks, return_exceptions=True)
            return names

        names = asyncio.run(_go())
        assert len(names) == 3
        assert any("_maintenance_task" in n for n in names)
        assert any("_loop_monitor_task" in n for n in names)
        assert any("_news_scheduler_task" in n for n in names)


# =========================================================
# R4 - single instance guard
# =========================================================


class TestSingleInstanceGuard:
    """Only a pid whose command line names this script may stop a start.

    start_bot.bat ends in `pause`, so refusing to start on a stale or recycled
    pid leaves no bridge at all, parked on a keypress nobody is there to give.
    Every uncertain case must therefore start.
    """

    def _holder(self, monkeypatch, state):
        monkeypatch.setattr(tb, "_lock_holder_state", lambda pid: state)

    def test_takes_over_from_a_dead_instance(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "bridge.pid"
        pid_file.write_text(str(os.getpid() + 1), encoding="utf-8")
        self._holder(monkeypatch, "gone")
        assert tb.acquire_single_instance(pid_file) is True
        assert pid_file.read_text(encoding="utf-8") == str(os.getpid())

    def test_recycled_pid_does_not_block_startup(self, tmp_path, monkeypatch):
        """S1: after a crash or reboot the recorded pid usually belongs to some
        unrelated process. Alive is not the same as 'is a bridge'."""
        pid_file = tmp_path / "bridge.pid"
        pid_file.write_text(str(os.getpid() + 1), encoding="utf-8")
        self._holder(monkeypatch, "other")
        assert tb.acquire_single_instance(pid_file) is True
        assert pid_file.read_text(encoding="utf-8") == str(os.getpid())

    def test_a_failed_probe_fails_open(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "bridge.pid"
        pid_file.write_text(str(os.getpid() + 1), encoding="utf-8")
        self._holder(monkeypatch, "unknown")
        assert tb.acquire_single_instance(pid_file) is True

    @pytest.mark.parametrize(
        "content", ["not-a-pid", "", "   ", "12.5", "-1", "\u00b2\u00b2", "\u0660\u0661"]
    )
    def test_an_unparseable_pid_file_never_blocks_startup(self, tmp_path, content):
        """R1: isdigit() alone is True for Unicode digit forms int() rejects,
        and an exception here would abort the start entirely."""
        pid_file = tmp_path / "bridge.pid"
        pid_file.write_text(content, encoding="utf-8")
        assert tb.acquire_single_instance(pid_file) is True
        assert pid_file.read_text(encoding="utf-8") == str(os.getpid())

    def test_unreadable_pid_file_never_blocks_startup(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "bridge.pid"
        pid_file.write_text("123", encoding="utf-8")

        def _boom(*a, **kw):
            raise OSError("permission denied")

        monkeypatch.setattr(type(pid_file), "read_text", _boom)
        assert tb.acquire_single_instance(pid_file) is True

    def test_own_pid_is_not_treated_as_a_rival(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "bridge.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self._holder(monkeypatch, "bridge")
        assert tb.acquire_single_instance(pid_file) is True


class TestLockHolderState:
    def test_a_live_non_bridge_process_is_other(self, monkeypatch):
        monkeypatch.setattr(tb, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(tb, "_process_command_line", lambda pid: "notepad.exe")
        assert tb._lock_holder_state(1234) == "other"

    def test_an_unreadable_command_line_is_unknown(self, monkeypatch):
        monkeypatch.setattr(tb, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(tb, "_process_command_line", lambda pid: None)
        assert tb._lock_holder_state(1234) == "unknown"


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
