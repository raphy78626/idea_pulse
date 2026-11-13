"""Tests for the /pains page: the extracted pains, readable at a glance. No network."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402
import pain_extract  # noqa: E402
import pains_page  # noqa: E402

BODY = ("Every month I spend hours matching bank transfers to invoices in a spreadsheet. "
        "We use Xero but it misses half. I'd happily pay for a fix.")


def reply(**fields):
    base = {"author_role": "has_pain", "is_pain": True, "persona": "agency owner, 40 clients",
            "job_to_be_done": "match bank transfers to invoices",
            "current_workaround": "manual spreadsheet", "tools_mentioned": ["Xero"],
            "cost_signal": "hours every month", "pay_signal": "I'd happily pay",
            "quote": "Every month I spend hours matching bank transfers to invoices"}
    base.update(fields)
    return json.dumps(base)


def chat_with(text):
    return lambda system, user, **kw: (text, "test/model")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    monkeypatch.setattr(pains_page, "DB", tmp_path / "ideas.db")
    from fastapi.testclient import TestClient
    return TestClient(ideapulse.app)


def seed(rows):
    """rows: list of (title, chat reply). Each becomes one extracted pain post."""
    ideapulse.save_items("Reddit pains", [
        (t, f"https://www.reddit.com/r/smallbusiness/comments/{i}/", "r/smallbusiness · 3 ↑ · 7 comments",
         "pain", BODY, f"user{i}") for i, (t, _) in enumerate(rows)])
    conn = ideapulse.db()
    for item_id, title, body in pain_extract.pending(conn):
        text = dict(rows)[title]
        pain_extract.save(conn, item_id, pain_extract.extract_one(title, body, chat=chat_with(text)))
    conn.close()


def test_page_shows_pains_with_quote_persona_and_link(client):
    seed([("Matching transfers to invoices", reply()),
          ("Our big launch day!", reply(is_pain=False, author_role="other"))])
    r = client.get("/pains")
    assert r.status_code == 200
    assert "agency owner, 40 clients" in r.text
    assert "Every month I spend hours matching bank transfers to invoices" in r.text
    assert "https://www.reddit.com/r/smallbusiness/comments/0/" in r.text
    assert "Our big launch day!" not in r.text, "NOT_PAIN is hidden by default"


def test_builder_tab_shows_rivals(client):
    seed([("We built a reconciliation tool", reply(author_role="builder")),
          ("Matching transfers to invoices", reply())])
    r = client.get("/pains?status=builder")
    assert "We built a reconciliation tool" in r.text
    assert "Matching transfers to invoices" not in r.text


def test_all_tab_shows_everything(client):
    seed([("Matching transfers to invoices", reply()),
          ("Our big launch day!", reply(is_pain=False, author_role="other"))])
    r = client.get("/pains?status=all")
    assert "Matching transfers to invoices" in r.text and "Our big launch day!" in r.text


def test_counts_appear_on_the_tabs(client):
    seed([("A", reply()), ("B", reply()), ("C", reply(author_role="builder"))])
    r = client.get("/pains")
    assert "Pains (2)" in r.text and "Builders (1)" in r.text


def test_tools_are_tallied(client):
    seed([("A", reply()), ("B", reply())])
    r = client.get("/pains")
    assert '<span class="tn">Xero</span><span class="tc">2</span>' in r.text


def test_clicking_a_tool_filters_to_posts_naming_it(client):
    seed([("Uses Xero", reply()),
          ("No tools here", reply(tools_mentioned=[]))])
    r = client.get("/pains?tool=xero")
    assert "Uses Xero" in r.text and "No tools here" not in r.text
    assert "Posts naming <b>xero</b>" in r.text


def test_headline_counts_people_and_rivals(client):
    seed([("A", reply()), ("B", reply()), ("C", reply(author_role="builder"))])
    assert "2 people describe a problem in their own work. One is already building a fix." \
        in client.get("/pains").text


def test_post_text_is_escaped(client):
    seed([("<script>alert(1)</script> invoices", reply())])
    r = client.get("/pains?status=all")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_empty_database_still_renders(client):
    r = client.get("/pains")
    assert r.status_code == 200
    assert "app.py extract" in r.text, "tells you how to fill it"


def test_dashboard_links_to_pains(client):
    assert 'href="/pains"' in client.get("/").text
