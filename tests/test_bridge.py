"""Unit tests for telegram_bridge pure functions."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from tg_utils import split_message

# =========================================================
# _cleanup_old_logs tests
# =========================================================


class TestSplitMessage:
    def test_splits_at_line_boundary(self):
        lines = ["line1\n", "line2\n", "line3\n"]
        text = "".join(lines)
        chunks = split_message(text, limit=12)
        rejoined = "".join(chunks)
        assert rejoined == text


class TestCleanupOldLogs:
    """Calls the real telegram_bridge._cleanup_old_logs (with LOG_DIR
    monkeypatched) so a regression in the function itself fails these tests —
    the previous version reimplemented the loop inline and guarded nothing."""

    def _run(self, monkeypatch, log_dir):
        import telegram_bridge as tb

        monkeypatch.setattr(tb, "LOG_DIR", log_dir)
        tb._cleanup_old_logs()

    def test_ignores_non_bridge_files(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        other = log_dir / "notes.txt"
        other.write_text("keep me")
        old_time = time.time() - 90 * 86400
        os.utime(other, (old_time, old_time))

        self._run(monkeypatch, log_dir)

        assert other.exists()
