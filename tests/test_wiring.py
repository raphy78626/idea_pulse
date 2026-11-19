"""Regression tests for the high-leverage IdeaPulse wiring. No network.

Covers:
  1. llm.parse_json() handles stray braces in surrounding prose without failing.
  2. Verified PAIN records (persona, job, workaround, cost/pay signal, quote) and
     post bodies flow into money evaluation and the /money board, while BUILDER
     and NOT_PAIN records are excluded.
  3. /pains links verified PAIN authors (never BUILDERs) straight into /outreach
     with prefilled author, persona, quote, thread URL, and a Reddit reply draft.
  4. Reproducible dependency manifests exist at repo root.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as ideapulse  # noqa: E402
import llm  # noqa: E402
import money  # noqa: E402
import pain_extract  # noqa: E402
import pains_page  # noqa: E402

PAIN_BODY = (
    "I run a 3-person bookkeeping shop. Every Friday I spend 4 hours manually "
    "chasing missing receipts from clients in Gmail and Slack. We use QuickBooks "
    "and Dext but clients still ignore reminders. I'd happily pay $49/mo for a "
    "simple WhatsApp nudge that files receipts automatically."
)

BUILDER_BODY = (
    "I'm researching a tool to automate receipt chasing for bookkeepers. "
    "Every Friday bookkeepers spend 4 hours manually chasing missing receipts. "
    "Would anyone pay for this?"
)


def pain_reply(**overrides):
    d = {
        "author_role": "has_pain",
        "is_pain": True,
        "persona": "3-person bookkeeping shop owner",
        "job_to_be_done": "collect missing client receipts every Friday",
        "current_workaround": "manual nudges in Gmail and Slack",
        "tools_mentioned": ["QuickBooks", "Dext"],
        "cost_signal": "4 hours every Friday",
        "pay_signal": "would happily pay $49/mo",
        "quote": "Every Friday I spend 4 hours manually chasing missing receipts from clients",
    }
    d.update(overrides)
    return json.dumps(d)


def money_reply(**overrides):
    d = {
        "earn_verdict": "GO",
        "what": "WhatsApp receipt-chasing concierge for small bookkeepers",
        "buyer": "3-person bookkeeping shop owners on QuickBooks",
        "offer": "₹2,000 / $29 one-week setup + first 20 client chases",
        "first_payment": 7,
        "capital": 0,
        "proof": "partial",
        "reason": "Buyer explicitly stated $49/mo willingness to pay.",
    }
    d.update(overrides)
    return json.dumps(d)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "ideas.db"
    for mod in (ideapulse, money, pains_page):
        monkeypatch.setattr(mod, "DB", db_path)
    monkeypatch.setattr(ideapulse, "seed_from_csv", lambda: None)
    ideapulse.db().close()
    from fastapi.testclient import TestClient
    return TestClient(ideapulse.app)


def seed_pain_and_builder():
    ideapulse.save_items("Reddit pains", [
        (
            "Chasing client receipts every Friday",
            "https://www.reddit.com/r/Bookkeeping/comments/abc123/",
            "r/Bookkeeping · 18 ↑ · 12 comments · “manually”",
            "pain",
            PAIN_BODY,
            "bookkeeper_sam",
        ),
        (
            "Researching a receipt-chasing app",
            "https://www.reddit.com/r/Bookkeeping/comments/def456/",
            "r/Bookkeeping · 4 ↑ · 2 comments · “manually”",
            "pain",
            BUILDER_BODY,
            "startup_builder",
        ),
    ])
    conn = ideapulse.db()
    rows = pain_extract.pending(conn)
    for item_id, title, body in rows:
        if "Researching" in title:
            rep = pain_reply(
                author_role="builder",
                is_pain=False,
                quote="Every Friday bookkeepers spend 4 hours manually chasing missing receipts",
            )
        else:
            rep = pain_reply()
        p = pain_extract.extract_one(title, body, chat=lambda s, u, **kw: (rep, "test/model"))
        pain_extract.save(conn, item_id, p)
    conn.close()


# ---- 1. llm.parse_json robustness -------------------------------------------

def test_parse_json_survives_braces_in_surrounding_prose():
    text = (
        "Using {rubric v2} here is the decision:\n"
        '{"earn_verdict": "GO", "what": "Receipt nudge bot", "capital": 0}\n'
        "Note: check {pricing} before sending."
    )
    parsed = llm.parse_json(text)
    assert parsed == {"earn_verdict": "GO", "what": "Receipt nudge bot", "capital": 0}


def test_parse_json_survives_trailing_brace_after_valid_object():
    text = '{"ok": true, "n": 3}\n(See {footnote} for details.)'
    assert llm.parse_json(text) == {"ok": True, "n": 3}


def test_parse_json_still_repairs_trailing_comma_with_extra_braces():
    text = 'Prefix {ignored} then {"ok": true, "items": [1, 2,],} suffix {also ignored}'
    assert llm.parse_json(text) == {"ok": True, "items": [1, 2]}


# ---- 2. pains -> money.py handoff -------------------------------------------

def test_money_evaluate_uses_verified_pain_context_and_skips_builders(client, monkeypatch):
    seed_pain_and_builder()
    prompts = []

    def fake_chat(system, user, **kw):
        prompts.append(user)
        return money_reply(), "test/model"

    monkeypatch.setattr(ideapulse, "_or_chat", fake_chat)

    r = client.post("/money/evaluate?limit=10")
    assert r.status_code == 200
    assert r.json()["evaluated"] == 1, "only verified PAIN should be evaluated, never BUILDER"

    assert len(prompts) == 1
    p = prompts[0]
    assert "3-person bookkeeping shop owner" in p
    assert "collect missing client receipts every Friday" in p
    assert "manual nudges in Gmail and Slack" in p
    assert "4 hours every Friday" in p
    assert "would happily pay $49/mo" in p
    assert "Every Friday I spend 4 hours manually chasing missing receipts from clients" in p

    # /money board shows the verified pain offer and hides the builder post
    page = client.get("/money").text
    assert "Chasing client receipts every Friday" in page
    assert "Researching a receipt-chasing app" not in page
    assert "WhatsApp receipt-chasing concierge for small bookkeepers" in page
    assert "/outreach?prefill=" in page


def test_money_pains_lane_shows_only_verified_pains(client):
    seed_pain_and_builder()
    ideapulse.save_items("Reddit business ideas", [
        ("Generic hustle idea", "https://www.reddit.com/r/sidehustle/comments/999/", "r/sidehustle · 5 ↑", "business"),
    ])
    t = client.get("/money?lane=pains").text
    assert "Chasing client receipts every Friday" in t
    assert "Researching a receipt-chasing app" not in t
    assert "Generic hustle idea" not in t


# ---- 3. /pains -> /outreach lead handoff ------------------------------------

def test_pains_page_links_pain_authors_to_outreach_but_not_builders(client):
    seed_pain_and_builder()
    pain_html = client.get("/pains?status=pain").text
    assert "Add as lead" in pain_html
    assert "/outreach?prefill=" in pain_html

    builder_html = client.get("/pains?status=builder").text
    assert "Add as lead" not in builder_html


def test_outreach_prefill_from_verified_pain_populates_lead_and_thread_action(client):
    seed_pain_and_builder()
    conn = ideapulse.db()
    pain_id = conn.execute("SELECT item_id FROM pains WHERE status='PAIN'").fetchone()[0]
    conn.close()

    r = client.get(f"/outreach?prefill={pain_id}")
    assert r.status_code == 200
    t = r.text
    assert "u/bookkeeper_sam" in t
    assert "3-person bookkeeping shop owner" in t
    assert "pain_reply" in t
    assert "https://www.reddit.com/r/Bookkeeping/comments/abc123/" in t
    assert "Every Friday I spend 4 hours manually chasing missing receipts from clients" in t

    # Saving the prefilled lead creates an actionable card with an Open thread button
    add_r = client.post("/outreach/add", data={
        "name": "u/bookkeeper_sam",
        "business": "3-person bookkeeping shop owner",
        "phone": "",
        "template": "pain_reply",
        "followup": "",
        "notes": (
            "Job: collect missing client receipts every Friday | "
            'Quote: "Every Friday I spend 4 hours manually chasing missing receipts from clients" | '
            "Thread: https://www.reddit.com/r/Bookkeeping/comments/abc123/"
        ),
    }, follow_redirects=False)
    assert add_r.status_code == 303

    out_html = client.get("/outreach").text
    assert "u/bookkeeper_sam" in out_html
    assert "Open thread" in out_html
    assert "https://www.reddit.com/r/Bookkeeping/comments/abc123/" in out_html
    assert "collect missing client receipts every Friday" in out_html

    # And /pains now recognizes this lead is already in outreach
    assert "In outreach" in client.get("/pains?status=pain").text


# ---- 4. Dependency manifests ------------------------------------------------

def test_requirements_manifests_exist():
    req = (ROOT / "requirements.txt").read_text()
    for pkg in ("fastapi", "uvicorn", "httpx"):
        assert pkg in req.lower()
    dev = (ROOT / "requirements-dev.txt").read_text()
    assert "pytest" in dev.lower()
