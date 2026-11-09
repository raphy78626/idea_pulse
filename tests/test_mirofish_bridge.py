"""Tests for mirofish_bridge. No network, no model, no subprocess."""
import mirofish_bridge


MIROFISH_REPORT = """# Future Forecast Report: Market Reaction

> The simulation predicts a polarized response.

## The Announced Idea and the Initial State

Apollo and ZoomInfo respond defensively.

## Key Forecasted Trends and Strategic Risks

Vendor lock-in is the central objection buyers raise.
"""


def test_accepts_mirofish_report_without_final_call_heading():
    # the deep-eval helper requires '## Final call'; MiroFish reports never have it
    assert mirofish_bridge.report_is_complete(MIROFISH_REPORT) is True


def test_rejects_empty_report():
    assert mirofish_bridge.report_is_complete("") is False


def test_rejects_short_report():
    assert mirofish_bridge.report_is_complete("# Hi\n\ntoo short") is False


def test_rejects_report_cut_off_mid_sentence():
    body = "# Report\n\n" + ("Sentence one is fine. " * 20) + "and then it stops mid"
    assert mirofish_bridge.report_is_complete(body) is False


def test_seed_includes_title_meta_and_source():
    seed = mirofish_bridge.build_seed("My idea", "some context", "Reddit")
    assert "My idea" in seed
    assert "some context" in seed
    assert "Reddit" in seed


def test_seed_unescapes_html_entities():
    seed = mirofish_bridge.build_seed("AT&amp;T app", "&quot;quoted&quot;", "HN")
    assert "AT&T app" in seed
    assert '"quoted"' in seed
    assert "&amp;" not in seed


def test_seed_tolerates_missing_meta():
    seed = mirofish_bridge.build_seed("Just a title", None, "Reddit")
    assert "Just a title" in seed
    assert len(seed) > 20


def test_seed_truncates_absurd_titles():
    seed = mirofish_bridge.build_seed("x" * 5000, "", "Reddit")
    assert len(seed) < 6000


def test_seed_includes_expansion_when_provided():
    seed = mirofish_bridge.build_seed("T", "m", "Reddit", expansion="Apollo, Clay, ZoomInfo")
    assert "Apollo, Clay, ZoomInfo" in seed


def test_seed_omits_expansion_header_when_absent():
    seed = mirofish_bridge.build_seed("T", "m", "Reddit")
    assert "Market participants" not in seed


def test_seed_includes_evaluation_excerpt():
    seed = mirofish_bridge.build_seed("T", "m", "Reddit", evaluation="Verdict: SKIP 2/10")
    assert "SKIP 2/10" in seed
