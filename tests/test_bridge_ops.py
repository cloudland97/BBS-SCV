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


class TestLogCommand:
    """!log end-to-end against a temp logs dir."""

    def _run(self, monkeypatch, tmp_path, text):
        monkeypatch.setattr(cd._cfg, "DISCORD_LOGS_PATH", tmp_path)
        replies = []

        class _Msg:
            text = None

            async def reply_text(self, t, **_kw):
                replies.append(t)

        msg = _Msg()
        msg.text = text
        update = type(
            "U",
            (),
            {"message": msg, "effective_user": type("X", (), {"id": ALLOWED_USER_ID})()},
        )()
        asyncio.run(cd.cmd_log(update, None))
        return replies

    def test_reads_todays_live_error_log(self, monkeypatch, tmp_path):
        (tmp_path / "error.log").write_text("[ERROR] boom\n", encoding="utf-8")
        replies = self._run(monkeypatch, tmp_path, "!log")
        assert any("boom" in r for r in replies)

    def test_reads_a_rotated_day(self, monkeypatch, tmp_path):
        target = (tb.datetime.now(cd._KST) - tb.timedelta(days=2)).strftime("%Y-%m-%d")
        (tmp_path / f"error.log.{target}").write_text("[ERROR] older\n", encoding="utf-8")
        replies = self._run(monkeypatch, tmp_path, "!log 2")
        assert any("older" in r for r in replies)

    def test_absurd_day_count_is_capped_not_crashed(self, monkeypatch, tmp_path):
        # N16: an unbounded N reached timedelta and raised OverflowError.
        replies = self._run(monkeypatch, tmp_path, "!log 999999999999")
        assert replies and "No error log" in replies[0]


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

    def test_result_never_escapes_the_temp_dir(self, tmp_path):
        target = tmp_path / tb._safe_media_filename("..\\..\\evil.txt")
        assert target.resolve().parent == tmp_path.resolve()

    @pytest.mark.parametrize("raw", [None, "", "..", ".", "/", "\\"])
    def test_empty_or_dot_names_get_a_generated_name(self, raw):
        name = tb._safe_media_filename(raw)
        assert name.startswith("media_")
        assert "/" not in name and "\\" not in name


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

    def test_nothing_is_written_into_the_bbs_project_tree(self, monkeypatch, tmp_path):
        _u, tg_file, _s = self._run(monkeypatch, tmp_path, _FakeMedia(file_name="a.png"))
        assert str(tb.PROJECTS["discord"]) not in tg_file.written[0]

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
    def test_prunes_old_logs_and_emits_a_heartbeat(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(tb, "LOG_DIR", tmp_path)
        old_log = tmp_path / "bridge_2025-01-01.log"
        old_log.write_text("old", encoding="utf-8")
        old = time.time() - 40 * 86400
        os.utime(old_log, (old, old))
        recent = tmp_path / "bridge_2026-09-01.log"
        recent.write_text("recent", encoding="utf-8")

        with caplog.at_level(logging.INFO, logger="telegram_bridge"):
            tb._run_daily_maintenance()

        assert not old_log.exists()
        assert recent.exists()
        assert any("heartbeat" in r.getMessage() for r in caplog.records)

    def test_a_cleanup_failure_still_emits_the_heartbeat(self, monkeypatch, caplog):
        def _boom():
            raise OSError("disk gone")

        monkeypatch.setattr(tb, "_cleanup_old_logs", _boom)
        with caplog.at_level(logging.INFO, logger="telegram_bridge"):
            tb._run_daily_maintenance()
        assert any("heartbeat" in r.getMessage() for r in caplog.records)

    def test_orphan_temp_dirs_are_swept_daily_too(self, tmp_path, monkeypatch):
        # S7: this ran only at boot, so a long-lived process never swept.
        monkeypatch.setattr(tb, "LOG_DIR", tmp_path)
        monkeypatch.setattr(tb.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(tb, "PROJECTS", {})
        stale = tmp_path / f"{tb._TEMP_PREFIX}old"
        stale.mkdir()
        old = time.time() - 7200
        os.utime(stale, (old, old))

        tb._run_daily_maintenance()

        assert not stale.exists()

    def test_startup_registers_all_background_tasks(self, tmp_path, monkeypatch):
        """Asserts the tasks actually exist, not that the source mentions them."""
        monkeypatch.setattr(tb, "LOG_DIR", tmp_path)
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

    def test_claims_a_free_pid_file(self, tmp_path):
        pid_file = tmp_path / "bridge.pid"
        assert tb.acquire_single_instance(pid_file) is True
        assert pid_file.read_text(encoding="utf-8") == str(os.getpid())

    def test_refuses_when_a_real_bridge_holds_it(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "bridge.pid"
        pid_file.write_text(str(os.getpid() + 1), encoding="utf-8")
        self._holder(monkeypatch, "bridge")
        assert tb.acquire_single_instance(pid_file) is False
        # The other instance's claim is left untouched.
        assert pid_file.read_text(encoding="utf-8") == str(os.getpid() + 1)

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


class TestReleaseSingleInstance:
    """R2: hand the lock back on a clean exit, but only if it is still ours."""

    def test_removes_our_own_pid_file(self, tmp_path):
        pid_file = tmp_path / "bridge.pid"
        tb.acquire_single_instance(pid_file)
        tb.release_single_instance(pid_file)
        assert not pid_file.exists()

    def test_leaves_a_file_another_instance_has_claimed(self, tmp_path):
        pid_file = tmp_path / "bridge.pid"
        other = str(os.getpid() + 1)
        pid_file.write_text(other, encoding="utf-8")
        tb.release_single_instance(pid_file)
        # Deleting this would hand a second poller the lock.
        assert pid_file.read_text(encoding="utf-8") == other

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert tb.release_single_instance(tmp_path / "absent.pid") is None

    def test_an_unreadable_file_never_breaks_shutdown(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "bridge.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        def _boom(*a, **kw):
            raise OSError("locked")

        monkeypatch.setattr(type(pid_file), "read_text", _boom)
        assert tb.release_single_instance(pid_file) is None


class TestLockHolderState:
    def test_a_live_non_bridge_process_is_other(self, monkeypatch):
        monkeypatch.setattr(tb, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(tb, "_process_command_line", lambda pid: "notepad.exe")
        assert tb._lock_holder_state(1234) == "other"

    def test_a_live_bridge_is_bridge(self, monkeypatch):
        monkeypatch.setattr(tb, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(
            tb, "_process_command_line", lambda pid: r"python.exe C:\x\telegram_bridge.py"
        )
        assert tb._lock_holder_state(1234) == "bridge"

    def test_a_dead_pid_is_gone(self, monkeypatch):
        monkeypatch.setattr(tb, "_pid_alive", lambda pid: False)
        assert tb._lock_holder_state(1234) == "gone"

    def test_an_empty_command_line_means_gone(self, monkeypatch):
        monkeypatch.setattr(tb, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(tb, "_process_command_line", lambda pid: "")
        assert tb._lock_holder_state(1234) == "gone"

    def test_an_unreadable_command_line_is_unknown(self, monkeypatch):
        monkeypatch.setattr(tb, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(tb, "_process_command_line", lambda pid: None)
        assert tb._lock_holder_state(1234) == "unknown"

    def test_impossible_pids_are_gone(self):
        assert tb._lock_holder_state(0) == "gone"
        assert tb._lock_holder_state(-1) == "gone"


class TestLivenessProbe:
    def test_reports_this_process_alive(self):
        # S9: WaitForSingleObject, not GetExitCodeProcess - a process that exited
        # with code 259 is indistinguishable from a running one under
        # STILL_ACTIVE.
        assert tb._pid_alive(os.getpid()) is True

    def test_reads_this_process_own_command_line(self):
        cmdline = tb._process_command_line(os.getpid())
        assert cmdline
        assert "python" in cmdline.lower()

    def test_a_dead_pid_reports_no_process(self):
        # Empty string, not None: "no such process", not "could not tell".
        assert tb._process_command_line(0x7FFFFFFE) == ""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows System process")
    def test_a_process_we_cannot_open_still_counts_as_alive(self):
        """R4: OpenProcess on pid 4 fails with ERROR_ACCESS_DENIED.

        A failed open is not proof of death; only ERROR_INVALID_PARAMETER means
        no such process. Reporting the System process dead would let a recycled
        pid look free.
        """
        assert tb._pid_alive(4) is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows System process")
    def test_a_null_command_line_is_unknown_not_gone(self):
        """R5: pid 4 exists but reports no CommandLine.

        'Cannot tell' has to fail open, so the caller starts the bridge rather
        than deleting a lock it could not verify.
        """
        assert tb._process_command_line(4) is None
        assert tb._lock_holder_state(4) == "unknown"


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

    def test_a_missing_root_aborts_instead_of_sweeping(self, monkeypatch):
        async def _to_thread(fn, *a, **kw):
            return type("R", (), {"stdout": "no-root", "stderr": ""})()

        async def _no_sleep(_s):
            return None

        monkeypatch.setattr(cd.asyncio, "to_thread", _to_thread)
        monkeypatch.setattr(cd, "_sleep", _no_sleep)
        monkeypatch.setattr(cd._cfg, "PROJECTS", {"discord": r"C:\DEV\BBS"})
        monkeypatch.setattr(cd._cfg, "DISCORD_LOGS_PATH", tb.Path("."))

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
        assert any("Restart aborted" in r for r in replies)


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

    def test_ok_schedules_a_delayed_forced_restart(self, monkeypatch):
        import cmd_moderation as cm

        calls, replies = self._run(monkeypatch, "!reboot OK")
        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv[0] == "shutdown" and "/r" in argv and "/f" in argv
        assert argv[argv.index("/t") + 1] == str(cm._REBOOT_DELAY_SEC)
        assert cm._REBOOT_DELAY_SEC >= 60
        # Console tools write the OEM code page; a bounded wait keeps the loop responsive.
        assert kwargs["encoding"] == "oem" and kwargs["errors"] == "replace"
        assert kwargs["capture_output"] is True and kwargs["timeout"] == 15
        assert any("재부팅" in r and "!reboot cc" in r for r in replies)

    def test_cc_aborts_the_pending_restart(self, monkeypatch):
        calls, replies = self._run(monkeypatch, "!reboot cc")
        assert [a for a, _ in calls] == [["shutdown", "/a"]]
        assert any("취소" in r for r in replies)

    def test_cc_with_nothing_pending_is_informational(self, monkeypatch):
        _, replies = self._run(monkeypatch, "!reboot cc", returncode=1116)
        assert any("없음" in r for r in replies)
        assert not any("실패" in r for r in replies)

    def test_second_ok_reports_the_running_countdown(self, monkeypatch):
        _, replies = self._run(monkeypatch, "!reboot ok", returncode=1190)
        assert any("이미" in r and "!reboot cc" in r for r in replies)
        assert not any("실패" in r for r in replies)

    def test_nonzero_exit_is_reported_not_swallowed(self, monkeypatch):
        calls, replies = self._run(monkeypatch, "!reboot ok", returncode=1)
        assert len(calls) == 1
        assert any("실패" in r and "boom" in r for r in replies)

    def test_subprocess_exception_still_answers(self, monkeypatch):
        import subprocess

        exc = subprocess.TimeoutExpired(cmd="shutdown", timeout=15)
        _, replies = self._run(monkeypatch, "!reboot ok", raise_exc=exc)
        assert any("실행 실패" in r for r in replies)
