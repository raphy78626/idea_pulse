"""Unit tests for X (Twitter) pain harvesting & critic-gated outreach (x_outreach.py). No network."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as ideapulse  # noqa: E402
import ideapulse_cli as cli  # noqa: E402
import money  # noqa: E402
import pains_page  # noqa: E402
import x_outreach as xo  # noqa: E402

GOOD_X_CRITIC = """VERDICT: GO — concrete Shopify CSV workflow pain.
OFFER: 24-hour custom Python bulk variant updater for $49 flat.
MESSAGE: Shopify's Admin GraphQL productVariantsBulkUpdate mutation updates all variant prices in one call. Happy to script a quick runner for your store if you want — feel free to DM.
"""

SKIP_X_CRITIC = """VERDICT: SKIP — casual comment, not a software pain.
OFFER: none
MESSAGE: none
"""


@pytest.fixture()
def seeded_x(tmp_path, monkeypatch):
    db_path = tmp_path / "ideas.db"
    for mod in (ideapulse, money, pains_page):
        monkeypatch.setattr(mod, "DB", db_path)
    monkeypatch.setattr(ideapulse, "seed_from_csv", lambda: None)

    fake_tweets = [
        {
            "user": "ShopOwner @shop_dan · 20m",
            "text": "I hate doing this manually in Shopify every week — updating compare-at prices across 40 variants takes hours.",
            "url": "https://x.com/shop_dan/status/2123300001",
            "time": "2026-11-19T09:00:00.000Z",
        },
        {
            "user": "AgencyOps @ops_maya · 1h",
            "text": "Is there a tool that matches Stripe payouts to QuickBooks invoices without spending 3 hours in a spreadsheet?",
            "url": "https://x.com/ops_maya/status/2123300002",
            "time": "2026-11-19T08:30:00.000Z",
        },
    ]
    res = xo.harvest_x_pains(fetch_fn=lambda **kw: fake_tweets)
    assert res["fetched"] == 2 and res["saved_new"] == 2
    cands = xo.candidate_x_items(limit=10)
    return [c["id"] for c in cands]


def test_validate_x_reply_enforces_length_no_links_no_hashtags_no_prices():
    ok, _ = xo.validate_x_reply("Admin GraphQL productVariantsBulkUpdate handles all variants in one call. Happy to script it if helpful — DM me.")
    assert ok is True

    bad_len, err_len = xo.validate_x_reply("word " * 70)
    assert not bad_len and "too long" in err_len

    bad_tag, err_tag = xo.validate_x_reply("Happy to help automate your Shopify workflow! #shopify #saas")
    assert not bad_tag and "hashtags" in err_tag

    bad_link, err_link = xo.validate_x_reply("Check https://example.com for a quick tool that solves this.")
    assert not bad_link and "links" in err_link

    bad_price, err_price = xo.validate_x_reply("I can build a quick Shopify script for $99 today, let me know!")
    assert not bad_price and "prices" in err_price


def test_x_harvest_is_idempotent_and_reply_respects_dry_run_send_dup_and_cap(seeded_x):
    # Re-harvesting same tweet URLs adds 0 duplicates
    dup_h = xo.harvest_x_pains(fetch_fn=lambda **kw: [{
        "user": "ShopOwner @shop_dan",
        "text": "I hate doing this manually in Shopify",
        "url": "https://x.com/shop_dan/status/2123300001",
        "time": "2026-11-19T09:00:00.000Z",
    }])
    assert dup_h["saved_new"] == 0

    item_id = seeded_x[0]
    posted = []

    # Critic SKIP does not post
    sk = xo.reply_to_tweet(
        item_id, send=True,
        critic_fn=lambda p, **k: (SKIP_X_CRITIC, "llm", ""),
        post_fn=lambda u, m: posted.append(u) or {"ok": True},
    )
    assert sk["ok"] is True and sk["action"] == "skipped" and posted == []

    # Dry run validates & returns crisp reply without posting
    dr = xo.reply_to_tweet(
        item_id, send=False,
        critic_fn=lambda p, **k: (GOOD_X_CRITIC, "llm", ""),
        post_fn=lambda u, m: posted.append(u) or {"ok": True},
    )
    assert dr["ok"] is True and dr["action"] == "dry_run" and posted == []
    assert len(dr["message"]) <= xo.MAX_REPLY_CHARS

    # Live send posts once, logs to outreach_log (channel='x'), marks prospect contacted
    sent = xo.reply_to_tweet(
        item_id, send=True,
        critic_fn=lambda p, **k: (GOOD_X_CRITIC, "llm", ""),
        post_fn=lambda u, m: posted.append(u) or {"ok": True, "account": "@raphy78626", "posted_url": u},
    )
    assert sent["ok"] is True and sent["action"] == "sent" and len(posted) == 1
    assert sent["sent_today"] == 1 and sent["prospect_id"] is not None

    out = cli.list_outreach(status="contacted")
    assert len(out) == 1 and out[0]["thread_url"].startswith("https://x.com/")

    # Duplicate guard blocks re-replying
    dup = xo.reply_to_tweet(
        item_id, send=True,
        critic_fn=lambda p, **k: (GOOD_X_CRITIC, "llm", ""),
        post_fn=lambda u, m: posted.append(u) or {"ok": True},
    )
    assert dup["ok"] is False and "Duplicate guard" in dup["error"] and len(posted) == 1

    # Daily cap = 1 blocks second tweet today
    cap_res = xo.run_x_batch(
        limit=2, send=True, daily_cap=1, gap_s=0,
        critic_fn=lambda p, **k: (GOOD_X_CRITIC, "llm", ""),
        post_fn=lambda u, m: posted.append(u) or {"ok": True},
    )
    assert cap_res["ok"] is False and "Daily X cap" in cap_res["error"]
