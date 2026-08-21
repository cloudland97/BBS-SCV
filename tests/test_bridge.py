"""Unit tests for telegram_bridge pure functions.

split_message is imported from tg_utils (the real source), so these tests guard
the actual chunking used by safe_reply rather than a drifting local copy.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tg_utils import split_message


# =========================================================
# split_message tests
# =========================================================


class TestSplitMessage:
    def test_short_text_single_chunk(self):
        assert split_message("hello") == ["hello"]

    def test_empty_string(self):
        assert split_message("") == [""]

    def test_exact_limit(self):
        text = "a" * 100
        assert split_message(text, limit=100) == [text]

    def test_splits_at_line_boundary(self):
        lines = ["line1\n", "line2\n", "line3\n"]
        text = "".join(lines)
        chunks = split_message(text, limit=12)
        rejoined = "".join(chunks)
        assert rejoined == text

    def test_long_single_line_hard_split(self):
        text = "x" * 250
        chunks = split_message(text, limit=100)
        assert len(chunks) == 3
        assert "".join(chunks) == text

    def test_preserves_all_content(self):
        text = "a\n" * 500
        chunks = split_message(text, limit=50)
        assert "".join(chunks) == text

    def test_multiline_respects_limit(self):
        text = "\n".join(f"line {i}" for i in range(100))
        chunks = split_message(text, limit=200)
        for chunk in chunks:
            assert len(chunk) <= 200


# =========================================================
# _cleanup_old_logs tests
# =========================================================


class TestCleanupOldLogs:
    """Calls the real telegram_bridge._cleanup_old_logs (with LOG_DIR
    monkeypatched) so a regression in the function itself fails these tests —
    the previous version reimplemented the loop inline and guarded nothing."""

    def _run(self, monkeypatch, log_dir):
        import telegram_bridge as tb

        monkeypatch.setattr(tb, "LOG_DIR", log_dir)
        tb._cleanup_old_logs()

    def test_deletes_old_logs(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        old_log = log_dir / "bridge_2025-01-01.log"
        old_log.write_text("old")
        old_time = time.time() - 40 * 86400
        os.utime(old_log, (old_time, old_time))
        new_log = log_dir / "bridge_2026-04-09.log"
        new_log.write_text("new")

        self._run(monkeypatch, log_dir)

        assert not old_log.exists()
        assert new_log.exists()

    def test_keeps_recent_logs(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        recent = log_dir / "bridge_2026-04-09.log"
        recent.write_text("recent")

        self._run(monkeypatch, log_dir)

        assert recent.exists()

    def test_ignores_non_bridge_files(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        other = log_dir / "notes.txt"
        other.write_text("keep me")
        old_time = time.time() - 90 * 86400
        os.utime(other, (old_time, old_time))

        self._run(monkeypatch, log_dir)

        assert other.exists()
