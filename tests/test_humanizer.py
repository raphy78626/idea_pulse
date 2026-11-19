"""Tests for humanizer.py (ig-human cleanup + 5-check AI-tell gate). No network."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import humanizer  # noqa: E402


ROBOTIC_DRAFT = (
    "You can update compare_at_price for all variants at once using Shopify's Admin API "
    "instead of editing each size manually. A short Python script can loop through the "
    "product variants and write the new price in one go. If your store has many products, "
    "batching by product ID helps avoid rate limits. Happy to script this for you if it "
    "helps — feel free to DM."
)

HUMAN_REWRITE = (
    "Yep, editing sizes one by one is brutal. Shopify's Admin GraphQL API lets you batch "
    "all variant compare_at_price fields in a single call instead of clicking through 40 rows. "
    "Honestly, I'd test the CSV export first since it's free, and if you're doing this every "
    "week I can whip up a quick Python script for it. Just ping me."
)


def test_clean_text_removes_em_dash_and_curly_quotes():
    cleaned, changes, _ = humanizer.clean_text("I’d help — it’s fast\u200b.")
    assert "—" not in cleaned
    assert "’" not in cleaned
    assert "\u200b" not in cleaned
    assert changes >= 1


def test_robotic_draft_is_flagged_and_rewrite_passes():
    before = humanizer.humanize_reply(ROBOTIC_DRAFT, rewrite_fn=None)
    assert "—" not in before["text"]
    assert before["verdict"] != "PASS", f"robotic uniform draft should not PASS: {before}"

    calls = []

    def fake_rewrite(draft, weakest, detail, max_chars=None):
        calls.append(weakest)
        return HUMAN_REWRITE

    after = humanizer.humanize_reply(ROBOTIC_DRAFT, rewrite_fn=fake_rewrite, max_rewrites=2)
    assert len(calls) >= 1
    assert after["verdict"] == "PASS", f"human rewrite should PASS: {after}"
    assert after["human_score"] >= 70.0
    assert after["human_score"] > before["human_score"]
