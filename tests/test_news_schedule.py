"""Unit tests for the cmd_news_schedule guarded claimed_at migration.

Covers idempotence and the legacy-table ALTER.

A temp sqlite db is swapped in via tg_config.BBS_DB_PATH — get_bbs_db() (and thus
db_write) read that global at call time, so the shared bbs.db is never touched.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tg_config
import cmd_news_schedule as cns


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bbs.db"
    monkeypatch.setattr(tg_config, "BBS_DB_PATH", db_path)
    cns._ensure_table()
    return db_path


def _cols() -> list[str]:
    conn = tg_config.get_bbs_db()
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(scheduled_news)").fetchall()]
    finally:
        conn.close()


# =========================================================
# claimed_at migration
# =========================================================


class TestMigration:
    def test_ensure_table_idempotent(self, temp_db):
        # Running the migration again must not error or duplicate the column.
        cns._ensure_table()
        cns._ensure_table()
        assert _cols().count("claimed_at") == 1

    def test_adds_column_to_legacy_table(self, tmp_path, monkeypatch):
        db_path = tmp_path / "bbs.db"
        monkeypatch.setattr(tg_config, "BBS_DB_PATH", db_path)
        # Pre-create the old schema (no claimed_at) to exercise the ALTER path.
        conn = tg_config.get_bbs_db()
        try:
            conn.execute(
                "CREATE TABLE scheduled_news ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
                "scheduled_at TEXT NOT NULL, created_at TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'pending')"
            )
            conn.commit()
        finally:
            conn.close()
        assert "claimed_at" not in _cols()
        cns._ensure_table()
        assert "claimed_at" in _cols()
