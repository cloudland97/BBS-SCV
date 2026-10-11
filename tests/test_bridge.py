"""Unit tests for telegram_bridge pure functions."""

import os
import sys

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
