"""Unit tests for critic-gated, anti-ban Reddit outreach (reddit_outreach.py). No network."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as ideapulse  # noqa: E402
import ideapulse_cli as cli  # noqa: E402
import money  # noqa: E402
import pain_extract  # noqa: E402
import pains_page  # noqa: E402
import reddit_outreach as ro  # noqa: E402

PAIN_BODY = (
    "I run a small agency using QuickBooks. I am up to almost $400s in fees for "
    "$200 in actual invoices. Looking for a cheaper alternative and willing to switch."
)
BUILDER_BODY = (
    "We built a cheaper QuickBooks alternative for small agencies tired of $400 fees."
)


def _pain_json(**kw):
    d = {
        "author_role": "has_pain",
        "is_pain": True,
        "persona": "small agency owner on QuickBooks",
        "job_to_be_done": "replace QuickBooks with cheaper invoicing",
        "current_workaround": "QuickBooks",
        "tools_mentioned": ["QuickBooks"],
        "cost_signal": "$400 in fees on $200 in invoices",
        "pay_signal": "willing to switch",
        "quote": "I am up to almost $400s in fees for $200 in actual invoices",
    }
    d.update(kw)
    return json.dumps(d)


GOOD_CRITIC = """1. **COMMON_SENSE:** QuickBooks has no free built-in ACH fee workaround; migrating recurring templates is genuinely tedious.
2. **VERDICT:** GO — clear billing pain and active willingness to switch.
3. **OFFER:** Done-for-you migration of recurring invoices to Stripe + Wave for $49 flat.
4. **MESSAGE:**
"Those NSF fees are brutal. Honestly, I'd test Wave or Invoice Ninja with ACH first since QuickBooks won't budge on reversal fees. If you've got a pile of recurring client templates to move over, I can script the migration for you so you don't re-type them by hand. Ping me if you want help."
"""

SKIP_CRITIC = """VERDICT: SKIP — too vague, no software fix.
OFFER: none
MESSAGE: none
"""


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    db_path = tmp_path / "ideas.db"
    for mod in (ideapulse, money, pains_page):
        monkeypatch.setattr(mod, "DB", db_path)
    monkeypatch.setattr(ideapulse, "seed_from_csv", lambda: None)

    ideapulse.save_items("Reddit pains", [
        ("Tired of QuickBooks fees",
         "https://www.reddit.com/r/smallbusiness/comments/qb1/",
         "r/smallbusiness · 15 ↑ · 9 comments", "pain", PAIN_BODY, "alice_qb"),
        ("Another QuickBooks pain",
         "https://www.reddit.com/r/smallbusiness/comments/qb2/",
         "r/smallbusiness · 9 ↑ · 4 comments", "pain", PAIN_BODY, "bob_qb"),
        ("We built a QB rival",
         "https://www.reddit.com/r/SaaS/comments/qb3/",
         "r/SaaS · 3 ↑ · 1 comments", "pain", BUILDER_BODY, "rival_qb"),
    ])
    conn = ideapulse.db()
    for item_id, title, body in pain_extract.pending(conn):
        if "rival" in title:
            rep = _pain_json(author_role="builder", is_pain=False,
                             quote="cheaper QuickBooks alternative for small agencies")
        else:
            rep = _pain_json()
        pain_extract.save(conn, item_id, pain_extract.extract_one(title, body, chat=lambda s, u, **k: (rep, "m")))
    pains = [r[0] for r in conn.execute("SELECT item_id FROM pains WHERE status='PAIN' ORDER BY item_id").fetchall()]
    builder = conn.execute("SELECT item_id FROM pains WHERE status='BUILDER'").fetchone()[0]
    conn.close()
    return {"pains": pains, "builder": builder}


def test_parse_critic_reply_extracts_verdict_offer_and_message():
    p = ro.parse_critic_reply(GOOD_CRITIC)
    assert p["verdict"] == "GO"
    assert "QuickBooks" in p["common_sense"]
    assert "Stripe + Wave" in p["offer"]
    assert p["message"].startswith("Those NSF fees are brutal.")
    assert not p["message"].endswith('"')

    s = ro.parse_critic_reply(SKIP_CRITIC)
    assert s["parsed"] is True and s["verdict"] == "SKIP"

    stub = ro.parse_critic_reply("Gemini said")
    assert stub["parsed"] is False


def test_unparseable_critic_stub_returns_error_not_silent_skip(seeded):
    r = ro.reply_to_pain(
        seeded["pains"][0], send=False,
        critic_fn=lambda p, **k: ("Gemini said", "gemini-chrome", ""),
    )
    assert r["ok"] is False and r["code"] == 3


def test_validate_reply_blocks_slop_prices_and_links():
    ok, _ = ro.validate_reply("Those fees are rough. Wave + ACH works cleanly — happy to help migrate one template if you DM me.")
    assert ok is True

    bad1, err1 = ro.validate_reply("VERDICT: GO\nMESSAGE: hi there")
    assert not bad1 and "verdict:" in err1.lower()

    bad2, err2 = ro.validate_reply("Check https://a.com for a fix right now!")
    assert not bad2 and "must not contain links" in err2

    bad3, _ = ro.validate_reply("Try my demo at https://foo.trycloudflare.com today!")
    assert not bad3

    bad4, err4 = ro.validate_reply("I can script this for $200 one-time if you want to hire me today.")
    assert not bad4 and "must not quote prices" in err4


def test_reply_refuses_builder_and_supports_dry_run_then_send_and_blocks_duplicate(seeded):
    # Builder is refused
    b = ro.reply_to_pain(seeded["builder"], send=False, critic_fn=lambda p, **k: (GOOD_CRITIC, "gemini", ""))
    assert b["ok"] is False and b["code"] == 2

    # Critic SKIP does not post
    posts = []
    sk = ro.reply_to_pain(
        seeded["pains"][0], send=True,
        critic_fn=lambda p, **k: (SKIP_CRITIC, "gemini", ""),
        post_fn=lambda u, m: posts.append((u, m)) or {"ok": True},
    )
    assert sk["ok"] is True and sk["action"] == "skipped" and posts == []

    # Dry run validates & returns draft without calling post_fn
    pass_hum = lambda t: {"text": t, "human_score": 82.0, "verdict": "PASS", "weakest": "voice"}
    flag_hum = lambda t: {"text": t, "human_score": 38.0, "verdict": "FLAGGED", "weakest": "burstiness"}

    dr = ro.reply_to_pain(
        seeded["pains"][0], send=False,
        critic_fn=lambda p, **k: (GOOD_CRITIC, "gemini", ""),
        post_fn=lambda u, m: posts.append((u, m)) or {"ok": True},
        humanize_fn=pass_hum,
    )
    assert dr["ok"] is True and dr["action"] == "dry_run" and posts == []
    assert dr["thread_url"].startswith("https://old.reddit.com/")
    assert dr["human_verdict"] == "PASS"

    # Robotic (FLAGGED) draft is blocked when send=True
    blocked = ro.reply_to_pain(
        seeded["pains"][0], send=True,
        critic_fn=lambda p, **k: (GOOD_CRITIC, "gemini", ""),
        post_fn=lambda u, m: posts.append((u, m)) or {"ok": True},
        humanize_fn=flag_hum,
    )
    assert blocked["ok"] is False and "Humanizer gate blocked" in blocked["error"] and posts == []

    # Live send posts once, logs to outreach_log, and marks prospect contacted
    sent = ro.reply_to_pain(
        seeded["pains"][0], send=True,
        critic_fn=lambda p, **k: (GOOD_CRITIC, "gemini", ""),
        post_fn=lambda u, m: posts.append((u, m)) or {"ok": True, "user": "BlueberryImaginary46", "posted_url": u},
        humanize_fn=pass_hum,
    )
    assert sent["ok"] is True and sent["action"] == "sent" and len(posts) == 1
    assert sent["sent_today"] == 1 and sent["prospect_id"] is not None

    out_rows = cli.list_outreach(status="contacted")
    assert len(out_rows) == 1 and out_rows[0]["name"] == "u/alice_qb"

    # Duplicate attempt is blocked
    dup = ro.reply_to_pain(
        seeded["pains"][0], send=True,
        critic_fn=lambda p, **k: (GOOD_CRITIC, "gemini", ""),
        post_fn=lambda u, m: posts.append((u, m)) or {"ok": True},
    )
    assert dup["ok"] is False and "Duplicate guard" in dup["error"] and len(posts) == 1


def test_daily_cap_stops_batch_and_platform_error_halts_immediately(seeded):
    # Daily cap = 1 allows only 1 post across 2 candidate pains
    calls = []
    pass_hum = lambda t: {"text": t, "human_score": 82.0, "verdict": "PASS", "weakest": "voice"}
    res = ro.run_batch(
        limit=2, send=True, daily_cap=1, gap_s=0,
        critic_fn=lambda p, **k: (GOOD_CRITIC, "gemini", ""),
        post_fn=lambda u, m: calls.append(u) or {"ok": True, "user": "u", "posted_url": u},
        sleep_fn=lambda s: None,
        humanize_fn=pass_hum,
    )
    assert res["ok"] is True and res["sent_now"] == 1 and len(calls) == 1

    # Second run hits daily cap immediately
    res2 = ro.run_batch(
        limit=2, send=True, daily_cap=1, gap_s=0,
        critic_fn=lambda p, **k: (GOOD_CRITIC, "gemini", ""),
        post_fn=lambda u, m: calls.append(u) or {"ok": True},
    )
    assert res2["ok"] is False and "Daily Reddit cap" in res2["error"]
