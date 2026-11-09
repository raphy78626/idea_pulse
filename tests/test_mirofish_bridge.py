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
