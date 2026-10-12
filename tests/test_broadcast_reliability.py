"""Discord broadcast: the latest-headline file survives an image-only broadcast."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord_helpers as dh


class TestLatestHeadlinePersistence:
    """An image-only broadcast has no text and must not blank the feed headline."""

    def test_an_empty_body_keeps_the_previous_headline(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dh, "DISCORD_DATA_PATH", tmp_path)
        target = tmp_path / "bongnews_latest.txt"
        target.write_text("earlier headline", encoding="utf-8")

        dh._save_latest("   \n  ")

        assert target.read_text(encoding="utf-8") == "earlier headline"
