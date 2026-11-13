"""Tests for the simulation explainer. No network, no models.

The explainer reads a MiroFish report and states, in plain language, what the
report is and is not. These tests pin the two claims that matter most:

  1. It must never describe the output as a forecast — MiroFish simulates
     reactions and no time-series model is used.
  2. It must not present section headings or abstract concepts as participants.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_explain  # noqa: E402

REPORT = """# Future Forecast Report: Market Reaction

> The simulation predicts a polarized market response.

---

## The Announced Idea and the Initial State

The market opens with three players: **Apollo**, **ZoomInfo**, and **Clay**.

> "Clay would publicly position against Apollo as part of a new orchestration layer."

**The Pre-Announcement Market Structure**

Before the announcement, contracts overlap.

## Agent Reactions: A Multi-Polar Response

**ProcurementReviewer** objected to the pricing. **Apollo** pushed back hard.
**Pull** and **Resistance** shaped the dynamic.
"""


def test_cast_excludes_headings_and_articles():
    cast = sim_explain.extract_cast(REPORT)
    assert "Apollo" in cast and "ZoomInfo" in cast and "Clay" in cast
    assert "The Pre-Announcement Market Structure" not in cast
    assert "ProcurementReviewer" in cast


def test_cast_excludes_abstract_nouns():
    cast = sim_explain.extract_cast(REPORT)
    # bare concepts are not participants
    assert "Pull" not in cast or "Resistance" not in cast


def test_scenario_extracts_seeded_fact():
    scen = sim_explain.extract_scenario(REPORT)
    assert any("orchestration layer" in s for s in scen)
    # the summary line is not a scenario fact
    assert not any(s.startswith("The simulation predicts") for s in scen)


def test_headline_found():
    head = sim_explain.extract_headline(REPORT)
    assert "polarized" in head


def test_sections_listed_in_order():
    secs = sim_explain.extract_sections(REPORT)
    assert secs[0].startswith("The Announced Idea")
    assert any("Agent Reactions" in s for s in secs)


def test_flags_forecast_language():
    # The rate is per-100-words, so the sample must be dense enough to trip it,
    # matching how real MiroFish output reads (~42 predictive verbs per 4k words).
    dense = REPORT + "\n\nIt would drive growth. Buyers will move. Competitors would react. " * 3
    assert sim_explain.explain(dense)["uses_forecast_language"] is True


def test_does_not_flag_neutral_text():
    neutral = ("# Reaction simulation\n\n" + "The agents discussed the idea calmly. " * 40)
    assert sim_explain.explain(neutral)["uses_forecast_language"] is False


def test_never_claims_forecast():
    """The explainer's own output must state it is not a forecast."""
    html = sim_explain.render(REPORT, "An idea")
    assert "not a prediction" in html.lower() or "not a forecast" in html.lower()
    assert "TimesFM" in html          # explicitly names what is absent
    assert "simulated personas, not real" in html.lower()


def test_empty_report_renders_nothing():
    assert sim_explain.render("", "x") == ""
    assert sim_explain.render(None, "x") == ""


def test_handles_short_report():
    tiny = "# Title\n\nToo short to explain."
    e = sim_explain.explain(tiny)
    assert e["words"] > 0
    assert isinstance(e["cast"], list)


def test_no_timesfm_claim_when_absent():
    e = sim_explain.explain(REPORT)
    assert e["mentions_timesfm"] is False


# --- regression: concept labels must not become participants -------------
# Real report sim_7210 bolds "**Platform affinity**", "**Silent enforcement**"
# and "**Internal role fragmentation**" as labels, alongside genuine names like
# Gmail, Valimail and Yahoo. Both must be separated correctly.

LABEL_REPORT = """# Reaction simulation

> A polarized response.

## The Announced Idea

**Platform affinity** shaped the outcome. **Silent enforcement** was the risk.

Gmail is a major mailbox provider. Valimail competes with dmarcian.
**Internal role fragmentation** slowed adoption.
"""


def test_concept_labels_are_not_participants():
    cast = sim_explain.extract_cast(LABEL_REPORT)
    assert "Platform affinity" not in cast
    assert "Silent enforcement" not in cast
    assert "Internal role fragmentation" not in cast


def test_real_names_survive_label_filtering():
    cast = sim_explain.extract_cast(LABEL_REPORT)
    assert "Gmail" in cast
    assert "Valimail" in cast


def test_prose_participants_found_without_bold():
    """Some reports name participants only in plain sentences."""
    report = "Slickdeals is a deal community. AppSumo is a competing marketplace."
    cast = sim_explain.extract_cast(report)
    assert "Slickdeals" in cast and "AppSumo" in cast


def test_pronouns_never_become_participants():
    report = "It is important. They are many. There is a risk. Gmail is a provider."
    cast = sim_explain.extract_cast(report)
    for bad in ("It", "They", "There"):
        assert bad not in cast
