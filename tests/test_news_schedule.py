"""Unit tests for the cmd_news_schedule claim-before-send pipeline.

Covers the atomic claim (first wins, second loses), the guarded claimed_at
migration (idempotent + legacy-table ALTER), and stale-'sending' detection.

A temp sqlite db is swapped in via tg_config.BBS_DB_PATH — get_bbs_db() (and thus
db_write) read that global at call time, so the shared bbs.db is never touched.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tg_config
import cmd_news_schedule as cns

UTC = timezone.utc


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bbs.db"
    monkeypatch.setattr(tg_config, "BBS_DB_PATH", db_path)
    cns._ensure_table()
    return db_path


def _insert(scheduled_at: datetime | None = None) -> int:
    return cns.save_scheduled("hello news", scheduled_at or datetime.now(UTC))


def _cols() -> list[str]:
    conn = tg_config.get_bbs_db()
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(scheduled_news)").fetchall()]
    finally:
        conn.close()


# =========================================================
# claim_for_send
# =========================================================


class TestClaimForSend:
    def test_first_claim_wins_second_loses(self, temp_db):
        nid = _insert()
        assert cns.claim_for_send(nid) is True
        # Row is now 'sending' — a second attempt (next cycle / other instance)
        # sees rowcount 0 and must skip.
        assert cns.claim_for_send(nid) is False

    def test_claim_flips_status_and_sets_claimed_at(self, temp_db):
        nid = _insert()
        cns.claim_for_send(nid)
        conn = tg_config.get_bbs_db()
        try:
            row = conn.execute(
                "SELECT status, claimed_at FROM scheduled_news WHERE id = ?", (nid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "sending"
        assert row["claimed_at"] is not None

    def test_claimed_row_not_returned_by_get_due(self, temp_db):
        nid = _insert(datetime.now(UTC) - timedelta(minutes=1))
        assert any(r["id"] == nid for r in cns.get_due(datetime.now(UTC)))
        cns.claim_for_send(nid)
        # get_due is pending-only; a claimed row must not be re-picked.
        assert not any(r["id"] == nid for r in cns.get_due(datetime.now(UTC)))


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


# =========================================================
# get_stale_sending
# =========================================================


class TestStaleSending:
    def test_returns_only_rows_older_than_threshold(self, temp_db):
        nid_old = _insert()
        nid_new = _insert()
        cns.claim_for_send(nid_old)
        cns.claim_for_send(nid_new)
        # Backdate the old claim to 10 minutes ago.
        old_iso = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()

        def _w(conn):
            conn.execute(
                "UPDATE scheduled_news SET claimed_at = ? WHERE id = ?", (old_iso, nid_old)
            )

        tg_config.db_write(_w)

        stale_ids = {r["id"] for r in cns.get_stale_sending(datetime.now(UTC) - timedelta(minutes=5))}
        assert nid_old in stale_ids
        assert nid_new not in stale_ids

    def test_ignores_pending_rows(self, temp_db):
        nid = _insert()  # pending, never claimed
        stale = cns.get_stale_sending(datetime.now(UTC) + timedelta(minutes=5))
        assert nid not in {r["id"] for r in stale}
