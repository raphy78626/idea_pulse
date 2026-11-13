"""Tests for stage 2: turning a pain post into a structured, verified pain. No network.

The rule that matters most: nothing the model says is trusted unless it can be
found in the post. A quote that is not in the post is dropped, and tools the
post never mentions are removed.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402
import pain_extract  # noqa: E402

POST_TITLE = "How do you track which bank transfer paid which invoice?"
POST_BODY = ("I run a small agency with 40 clients. Every month I spend hours matching "
             "bank transfers to invoices in a spreadsheet because clients never put the "
             "invoice number in the reference. We use Xero but its matching misses half. "
             "I'd happily pay for something that just does this.")


def reply(**fields):
    base = {"is_pain": True, "persona": "small agency owner",
            "job_to_be_done": "match bank transfers to invoices",
            "current_workaround": "manual matching in a spreadsheet",
            "tools_mentioned": ["Xero"], "cost_signal": "hours every month",
            "pay_signal": "I'd happily pay",
            "quote": "Every month I spend hours matching bank transfers to invoices in a spreadsheet"}
    base.update(fields)
    return json.dumps(base)


def fake_chat(text, model="test/model"):
    calls = []

    def chat(system, user, **kw):
        calls.append(user)
        return text, model
    chat.calls = calls
    return chat


# ---- quote verification ------------------------------------------------------

def test_exact_quote_is_verified():
    assert pain_extract.verify_quote("clients never put the invoice number in the reference",
                                     POST_BODY)


def test_quote_survives_case_whitespace_and_curly_quotes():
    src = "I’d   happily pay for\nsomething that just does this."
    assert pain_extract.verify_quote("i'd happily pay for something that just does this", src)


def test_paraphrased_quote_is_rejected():
    assert not pain_extract.verify_quote("I waste hours reconciling payments every month",
                                         POST_BODY)


def test_tiny_quote_is_rejected():
    assert not pain_extract.verify_quote("Xero", POST_BODY)


# ---- one post -> one pain ------------------------------------------------------

def test_real_pain_with_verified_quote():
    p = pain_extract.extract_one(POST_TITLE, POST_BODY, chat=fake_chat(reply()))
    assert p["status"] == "PAIN"
    assert p["quote"].startswith("Every month I spend hours")
    assert p["persona"] == "small agency owner"
    assert p["tools"] == ["Xero"]
    assert p["model"] == "test/model"


def test_invented_quote_is_dropped_and_marked_unverified():
    text = reply(quote="Reconciliation is the worst part of running my business")
    p = pain_extract.extract_one(POST_TITLE, POST_BODY, chat=fake_chat(text))
    assert p["status"] == "UNVERIFIED"
    assert p["quote"] == ""


def test_tools_not_in_the_post_are_removed():
    text = reply(tools_mentioned=["Xero", "QuickBooks", "Stripe"])
    p = pain_extract.extract_one(POST_TITLE, POST_BODY, chat=fake_chat(text))
    assert p["tools"] == ["Xero"]


def test_quote_may_come_from_the_title():
    text = reply(quote="How do you track which bank transfer paid which invoice")
    p = pain_extract.extract_one(POST_TITLE, POST_BODY, chat=fake_chat(text))
    assert p["status"] == "PAIN"


def test_not_a_pain():
    p = pain_extract.extract_one("I built a tool for X", "Launching today!",
                                 chat=fake_chat(reply(is_pain=False)))
    assert p["status"] == "NOT_PAIN"


def test_founder_researching_a_fix_is_a_builder():
    """'I'm researching a tool for X' — the pain is real but the author is a rival."""
    text = reply(author_role="builder", is_pain=False)
    p = pain_extract.extract_one(POST_TITLE, POST_BODY, chat=fake_chat(text))
    assert p["status"] == "BUILDER"
    assert p["job"] == "match bank transfers to invoices", "kept so step 3 can count rivals"
    assert p["quote"].startswith("Every month"), "verified quote is kept"


def test_launch_told_as_a_story_is_a_builder_even_if_called_a_pain():
    text = reply(author_role="builder", is_pain=True)
    p = pain_extract.extract_one(POST_TITLE, POST_BODY, chat=fake_chat(text))
    assert p["status"] == "BUILDER"


def test_builder_with_invented_quote_drops_the_quote():
    text = reply(author_role="builder", quote="We are building the best reconciliation app")
    p = pain_extract.extract_one(POST_TITLE, POST_BODY, chat=fake_chat(text))
    assert p["status"] == "BUILDER" and p["quote"] == ""


def test_redo_reprocesses_finished_rows(conn):
    pain_extract.run(conn, limit=10, chat=fake_chat(reply()))
    assert pain_extract.pending(conn) == []
    s = pain_extract.run(conn, limit=10, chat=fake_chat(reply(author_role="builder")), redo=True)
    assert s["done"] == 2 and s["BUILDER"] == 2


def test_string_false_is_not_a_pain():
    p = pain_extract.extract_one(POST_TITLE, POST_BODY, chat=fake_chat(reply(is_pain="false")))
    assert p["status"] == "NOT_PAIN"


def test_model_failure_is_error_not_skip():
    p = pain_extract.extract_one(POST_TITLE, POST_BODY,
                                 chat=fake_chat("", model="nvidia/x: 429 rate limit"))
    assert p["status"] == "ERROR"
    assert "429" in p["error"]


def test_long_body_is_truncated_before_sending():
    chat = fake_chat(reply())
    pain_extract.extract_one(POST_TITLE, "word " * 5000, chat=chat)
    assert len(chat.calls[0]) < pain_extract.MAX_BODY + 500


# ---- the pending queue and storage -----------------------------------------------

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    ideapulse.save_items("Reddit pains", [
        (POST_TITLE, "https://www.reddit.com/r/a/comments/1/", "r/a", "pain", POST_BODY, "alice"),
        ("Second pain", "https://www.reddit.com/r/a/comments/2/", "r/a", "pain", "body two", "bob"),
    ])
    ideapulse.save_items("HN Show HN", [("Show HN: a launch", "https://x.dev", "5 pts", "show_hn")])
    c = ideapulse.db()
    yield c
    c.close()


def test_pending_only_returns_pain_items(conn):
    ids = [r[0] for r in pain_extract.pending(conn)]
    assert len(ids) == 2


def test_run_stores_results_and_does_not_redo_them(conn):
    summary = pain_extract.run(conn, limit=10, chat=fake_chat(reply()))
    assert summary["done"] == 2
    rows = conn.execute("SELECT item_id, status, quote, tools FROM pains ORDER BY item_id").fetchall()
    assert rows[0][1] == "PAIN" and rows[0][2].startswith("Every month")
    assert json.loads(rows[0][3]) == ["Xero"]
    assert rows[1][1] == "UNVERIFIED", "quote is not in 'body two'"
    assert pain_extract.pending(conn) == []


def test_errors_are_retried_on_the_next_run(conn):
    pain_extract.run(conn, limit=10, chat=fake_chat("", model="x: HTTP 500"))
    assert len(pain_extract.pending(conn)) == 2
    pain_extract.run(conn, limit=10, chat=fake_chat(reply()))
    statuses = {r[0] for r in conn.execute("SELECT status FROM pains")}
    assert "ERROR" not in statuses


def test_limit_is_respected(conn):
    summary = pain_extract.run(conn, limit=1, chat=fake_chat(reply()))
    assert summary["done"] == 1
    assert len(pain_extract.pending(conn)) == 1
