"""Tests for the main dashboard at /. No network."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    ideapulse.save_items("HN Show HN", [
        ("<b>Invoice matcher</b>", "https://x.dev/1", "5 pts · 2 comments", "show_hn"),
        ("A launch nobody needs", "https://x.dev/2", "1 pts", "show_hn"),
    ])
    conn = ideapulse.db()
    conn.execute("UPDATE items SET verdict='BUILD', buildability=8, ttm_days=5, what='Match bank"
                 " transfers to invoices' WHERE url='https://x.dev/1'")
    conn.commit(); conn.close()
    ideapulse.set_status("Dead feed", "EMPTY: returned nothing", 0)
    from fastapi.testclient import TestClient
    return TestClient(ideapulse.app)


def test_build_entry_shows_score_and_idea(client):
    t = client.get("/").text
    assert "Buildable 8 of 10, about 5 days to ship." in t
    assert "Match bank transfers to invoices" in t
    assert "&lt;b&gt;Invoice matcher&lt;/b&gt;" in t, "titles are escaped"


def test_headline_counts(client):
    assert "1 is worth building and 0 worth watching; 1 hasn’t been read yet." in client.get("/").text


def test_source_rail_flags_a_dead_feed(client):
    t = client.get("/").text
    assert "Dead feed" in t and "returned nothing" in t
    assert "1 need attention" in t


def test_source_filter_keeps_verdict_filter(client):
    t = client.get("/?filter=build&source=HN%20Show%20HN").text
    assert "Only ideas from <b>HN Show HN</b>" in t
    assert "A launch nobody needs" not in t
