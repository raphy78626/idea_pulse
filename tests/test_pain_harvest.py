"""Tests for the Reddit pain harvest and for loud source failures. No network.

What matters:
  - only posts that describe a pain survive the phrase filter
  - the post body and author are stored, not just the title
  - requests are paced, and a "slow down" answer is retried, not dropped
  - a source that returns nothing shows as EMPTY / FAILED, never as "ok"
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402
import pain_harvest  # noqa: E402


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    """Answers each GET from a queue of (status, payload); records the params."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def get(self, url, params=None, **kw):
        self.calls.append(params or {})
        status, payload = self.answers.pop(0)
        return FakeResp(status, payload)


def post(pid, title, body="", author="someone", sub="smallbusiness"):
    return {"id": pid, "title": title, "selftext": body, "author": author,
            "subreddit": sub, "score": 3, "num_comments": 7, "created_utc": 1794548415}


def ok(*posts):
    return 200, {"data": list(posts), "error": None}


SLOW = (422, {"data": None, "error": "Timeout. Maybe slow down a bit"})


# ---- the pain filter -------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Is there a tool that reconciles Stripe payouts?",
    "I spend hours every week chasing late invoices",
    "I'd happily pay for something that does this",
    "We do all of this manually in a spreadsheet",
    "Looking for an alternative to QuickBooks",
    "How do you all track unpaid invoices?",
])
def test_pain_phrases_match(text):
    assert pain_harvest.pain_signal(text)


@pytest.mark.parametrize("text", [
    "First client just paid us, so happy!",
    "Microsoft 365 outage yesterday",
    "Offering Excel services, DM me",
])
def test_non_pain_text_is_ignored(text):
    assert not pain_harvest.pain_signal(text)


# ---- fetching ---------------------------------------------------------------

def test_keeps_only_pain_posts_with_body_and_author():
    client = FakeClient([ok(
        post("a1", "Is there a tool for receipt matching?", "We match 400 receipts by hand", "alice"),
        post("a2", "Celebrating our first sale!"),
        post("a3", "Payroll question", "I spend hours on this every Friday", "bob"),
    )])
    items = pain_harvest.fetch(client, subs=["smallbusiness"], sleep=lambda s: None)
    assert [i[0] for i in items] == ["Is there a tool for receipt matching?", "Payroll question"]
    title, url, meta, kind, body, author = items[0]
    assert url == "https://www.reddit.com/r/smallbusiness/comments/a1/"
    assert kind == "pain"
    assert body == "We match 400 receipts by hand"
    assert author == "alice"
    assert "r/smallbusiness" in meta and "is there a tool" in meta


def test_cross_post_by_same_author_counts_once():
    client = FakeClient([
        ok(post("e1", "How do you reconcile profit manually?", author="sam", sub="a")),
        ok(post("e2", "How do you reconcile profit manually?", author="sam", sub="b"),
           post("e3", "How do you reconcile profit manually?", author="kim", sub="b")),
    ])
    items = pain_harvest.fetch(client, subs=["a", "b"], sleep=lambda s: None)
    assert [i[5] for i in items] == ["sam", "kim"]


def test_removed_body_is_not_stored_as_text():
    client = FakeClient([ok(post("b1", "Is there a tool for this?", "[removed]"))])
    items = pain_harvest.fetch(client, subs=["smallbusiness"], sleep=lambda s: None)
    assert items[0][4] == ""


def test_requests_are_paced_between_subreddits():
    waits = []
    client = FakeClient([ok(), ok(), ok()])
    pain_harvest.fetch(client, subs=["a", "b", "c"], sleep=waits.append, pause=4.0)
    assert waits == [4.0, 4.0], "one pause between each pair of requests"
    assert all(c["sort"] == "desc" for c in client.calls)


def test_slow_down_answer_is_retried():
    waits = []
    client = FakeClient([SLOW, ok(post("c1", "Is there an app for scheduling?"))])
    items = pain_harvest.fetch(client, subs=["smallbusiness"], sleep=waits.append)
    assert len(items) == 1
    assert len(client.calls) == 2
    assert waits and max(waits) >= pain_harvest.BACKOFF


def test_one_failing_subreddit_does_not_lose_the_rest():
    client = FakeClient([(500, {}), ok(post("d1", "Any tool to automate this manually?"))])
    items = pain_harvest.fetch(client, subs=["broken", "fine"], sleep=lambda s: None)
    assert len(items) == 1


def test_every_subreddit_failing_raises():
    client = FakeClient([(500, {}), (500, {})])
    with pytest.raises(RuntimeError):
        pain_harvest.fetch(client, subs=["x", "y"], sleep=lambda s: None)


# ---- storage and source status -------------------------------------------------

@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    return tmp_path


def test_save_items_stores_body_and_author(tmpdb):
    n = ideapulse.save_items("Reddit pains", [
        ("Is there a tool?", "https://www.reddit.com/r/x/comments/1/", "r/x", "pain",
         "full body text", "alice"),
        ("Old-style row", "https://example.com/2", "meta", "post"),
    ])
    assert n == 2
    conn = ideapulse.db()
    rows = conn.execute("SELECT title, body, author FROM items ORDER BY id").fetchall()
    conn.close()
    assert rows == [("Is there a tool?", "full body text", "alice"),
                    ("Old-style row", None, None)]


def _status_of(name):
    return {s["source"]: s["status"] for s in ideapulse.get_statuses()}[name]


def test_source_returning_nothing_is_marked_empty_not_ok(tmpdb, monkeypatch):
    monkeypatch.setattr(ideapulse, "discover_sources", lambda: [("Dead feed", lambda c: [])])
    ideapulse.fetch_all()
    assert _status_of("Dead feed").startswith("EMPTY")


def test_source_with_only_known_items_is_still_ok(tmpdb, monkeypatch):
    row = [("Same post", "https://example.com/same", "m", "post")]
    monkeypatch.setattr(ideapulse, "discover_sources", lambda: [("Steady feed", lambda c: row)])
    ideapulse.fetch_all()
    ideapulse.fetch_all()
    assert _status_of("Steady feed") == "ok"


def test_reddit_200_with_non_json_body_is_a_failure(tmpdb, monkeypatch):
    """old.reddit answers 200 with an HTML page; that is not a working source."""
    class HtmlResp:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    class Client:
        def get(self, url, **kw):
            return HtmlResp()

    monkeypatch.setattr(ideapulse, "SUBREDDITS", ["a"])
    with pytest.raises(RuntimeError, match="ValueError"):
        ideapulse.fetch_reddit(Client())


def test_reddit_blocked_everywhere_is_a_failure(tmpdb, monkeypatch):
    monkeypatch.setattr(ideapulse, "SUBREDDITS", ["a", "b"])
    client = FakeClient([(403, {})] * 4)
    with pytest.raises(RuntimeError, match="403"):
        ideapulse.fetch_reddit(client)
