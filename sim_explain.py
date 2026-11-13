"""Explain a MiroFish reaction simulation in plain terms.

The raw report is dense analyst prose addressed to someone who already knows
what it is. Measured on a real 4,243-word report: 40+ instances of "will",
titled "Future Forecast Report". Neither is accurate:

  - It is not a forecast. MiroFish runs LLM personas on a simulated social
    graph and reports how they reacted to an announcement. It is a structured
    role-play, not a prediction of real buyers.
  - It does not use TimesFM. There is no time-series model anywhere in the
    pipeline, and the design doc excludes it because no per-keyword history
    exists to forecast (see docs/.../2026-11-09-ideapulse-mirofish-design.md §8).

This module reads the report back and produces:
  1. A plain-language "what this is" block, stating the limits first.
  2. The concrete scenario the simulation ran (cast, idea, market).
  3. The main reactions, one line per side.
  4. What the report does NOT tell you.

Everything here is extracted from the report text by pattern, no model call, so
it is instant, free, and cannot hallucinate a claim the report does not contain.
"""
import re

# The report labels its own scenario facts as blockquotes; the simulation's
# cast is introduced with bold names. Both are stable in MiroFish output but
# every extractor below degrades to empty rather than raising.
_BOLD = re.compile(r"\*\*([A-Z][A-Za-z0-9&.\- ]{1,40})\*\*")
_QUOTE = re.compile(r"^>\s*\"?(.+?)\"?$", re.M)
_HEADING = re.compile(r"^#{2,3}\s+(.+)$", re.M)
# "Slickdeals is a deal aggregation community...", "AppSumo competes with..."
# Requires a proper-noun shape (mixed case or multi-syllable brand), so sentence
# openers like "It", "They" and "There" are rejected by _looks_like_participant.
_PROSE_ACTOR = re.compile(
    r"(?:^|[>\"“.;:]\s*)"
    r"([A-Z][A-Za-z0-9&.\-]{2,28})\s+"
    r"(?:is|are|competes|compete|targets|offers|sells|operates|positions|"
    r"launched|provides|serves|represents|remains)\b",
    re.M)

# Words that start sentences but are never a participant.
_PRONOUNS = {"it", "they", "there", "this", "that", "these", "those", "we",
             "you", "he", "she", "the", "a", "an", "its", "their", "our",
             "his", "her", "both", "each", "such", "what", "when", "where",
             "while", "with", "without", "after", "before", "however"}

CAST_STOPWORDS = {
    "the", "a", "an", "this", "that", "these", "those", "what", "why", "how",
    "buyers", "sellers", "market", "report", "simulation", "summary", "idea",
}

# Bold text in these reports is used for both participant names and for
# section labels, so filter on shape: a heading-like phrase has a capitalised
# function word, or repeats a section title already seen.
_NON_NAME_WORDS = {"the", "a", "an", "of", "and", "for", "in", "on", "to", "with"}

# Bolded abstractions that appear in these reports alongside real participants.
_ABSTRACT_NOUNS = {
    "pull", "resistance", "friction", "momentum", "signal", "pressure",
    "outcome", "trajectory", "tension", "risk", "opportunity", "verdict",
    "summary", "conclusion", "insight", "takeaway", "context", "challenge",
    "response", "reaction", "feedback", "demand", "supply", "value", "price",
    "growth", "launch", "announcement", "strategy", "positioning", "narrative",
    "affinity", "enforcement", "fragmentation", "adoption", "awareness",
    "skepticism", "resistance", "trust", "loyalty", "retention", "churn",
    "compliance", "complexity", "ambiguity", "uncertainty", "vulnerability",
    "exposure", "leverage", "incentive", "alignment", "dependence",
}

# Multi-word concept labels: "Platform affinity", "Silent enforcement",
# "Internal role fragmentation". Real participant names do not end in these.
_CONCEPT_HEADS = {
    "affinity", "enforcement", "fragmentation", "adoption", "awareness",
    "skepticism", "trust", "loyalty", "retention", "churn", "compliance",
    "complexity", "ambiguity", "uncertainty", "vulnerability", "exposure",
    "leverage", "incentive", "alignment", "dependence", "risk", "trend",
    "pressure", "dynamic", "signal", "gap", "shift",
}


def _sentences(text):
    # keep it simple: split on sentence ends, ignore abbreviations loosely
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _looks_like_participant(name, headings):
    words = name.split()
    if not words or len(words) > 3:
        return False
    if name in headings:
        return False
    # pronouns and sentence openers are never participants
    if name.lower() in _PRONOUNS or words[0].lower() in _PRONOUNS:
        return False
    # "The Pre-Announcement Market Structure" starts with an article
    if words[0].lower() in {"the", "a", "an"}:
        return False
    # a real name rarely carries 2+ capitalised function words ("And", "Of")
    caps_fn = sum(1 for w in words if w.lower() in _NON_NAME_WORDS)
    if caps_fn >= 2:
        return False
    # headings often end in these generic nouns
    if words[-1].lower() in {"structure", "dynamic", "reaction", "interactions",
                             "response", "analysis", "summary", "state", "outlook",
                             "trajectory", "outcome", "trend", "risk", "scenario"}:
        return False
    # any multi-word phrase containing a non-participant word is a label
    if any(w.lower() in {"forecasted", "predicted", "expected", "net", "overall",
                         "initial", "final", "key", "main", "total"} for w in words):
        return False
    # abstract concepts get bolded as often as names do; a participant is
    # either a single CamelCase/hyphenated token or a role described in words
    if len(words) == 1 and not (name[:1].isupper() and (
            any(c.isupper() for c in name[1:]) or   # CamelCase: ProcurementReviewer
            any(c.isdigit() for c in name) or       # Company2
            "-" in name)):                          # start-up
        if name.lower() in _ABSTRACT_NOUNS:
            return False
    # multi-word concept label: "Platform affinity", "Silent enforcement"
    if len(words) > 1 and words[-1].lower() in _CONCEPT_HEADS:
        return False
    return True


def extract_cast(report, limit=10):
    """Names the report treats as market participants.

    Two report styles exist in practice:
      - some bold entity names (**Apollo**, **ZoomInfo**)
      - others bold only labels (**Buyer-side pressure:**) and name participants
        in plain prose ("Slickdeals is a deal aggregation community...")
    Relying on bold alone produced "0 participants" for the second style, so we
    also collect names from the scenario sentences themselves.
    """
    headings = set(h.strip() for h in _HEADING.findall(report or ""))
    counts = {}

    for m in _BOLD.finditer(report or ""):
        name = m.group(1).strip().rstrip(":. ")
        if len(name) < 3 or name.lower() in CAST_STOPWORDS:
            continue
        if not _looks_like_participant(name, headings):
            continue
        counts[name] = counts.get(name, 0) + 1

    # Prose style: "<Name> is a/an <description>", "<Name> competes with",
    # "<Name> targets ...". Require a capitalised multi-char proper noun and a
    # market-ish predicate so ordinary sentence starts are not captured.
    for m in _PROSE_ACTOR.finditer(report or ""):
        name = m.group(1).strip()
        if not name or name in headings or name.lower() in CAST_STOPWORDS:
            continue
        if not _looks_like_participant(name, headings):
            continue
        counts[name] = counts.get(name, 0) + 1

    return [n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


def extract_scenario(report, limit=3):
    """The quoted 'scenario facts' the simulation was seeded with."""
    out = []
    for m in _QUOTE.finditer(report or ""):
        line = m.group(1).strip()
        if len(line) < 40 or line.startswith("The simulation"):
            continue
        if line not in out:
            out.append(line)
        if len(out) >= limit:
            break
    return out


def extract_headline(report):
    """The report's own one-line summary (the first blockquote after the title)."""
    m = re.search(r"^>\s*(?!\")(.+)$", report or "", re.M)
    if m:
        line = m.group(1).strip()
        if len(line) > 40:
            return line
    for s in _sentences(report or ""):
        if len(s) > 60:
            return s
    return ""


def extract_sections(report, limit=6):
    """Section headings, so the reader can see the shape of the argument."""
    return [h.strip() for h in _HEADING.findall(report or "")][:limit]


def explain(report, idea_title=""):
    """Build the explainer block: dict of plain-language facts about the report."""
    report = report or ""
    words = len(report.split())
    cast = extract_cast(report)
    scenario = extract_scenario(report)
    headline = extract_headline(report)
    sections = extract_sections(report)

    # Honesty checks run on the text itself, not on assumptions. MiroFish writes
    # "would" and "will" interchangeably, so both count as predictive framing.
    forecast_words = len(re.findall(
        r"\b(will|would|won't|will not|is likely to|are likely to)\b", report, re.I))
    mentions_timesfm = bool(re.search(r"times\s*fm|time-series", report, re.I))
    # Scale to length: a 4,000-word report with 40 "will"s is forecast-flavoured,
    # but so is a 60-word fixture with 2 of them. Use a rate, not an absolute.
    rate = forecast_words / max(1, words / 100.0)

    return {
        "idea_title": idea_title,
        "words": words,
        "cast": cast,
        "scenario": scenario,
        "headline": headline,
        "sections": sections,
        "forecast_words": forecast_words,
        "uses_forecast_language": forecast_words >= 2 and rate >= 0.4,
        "mentions_timesfm": mentions_timesfm,
        "is_forecast": False,   # by construction; stated so the UI can say it plainly
    }


# ---------------------------------------------------------------- rendering

def _esc(s):
    import html
    return html.escape(str(s or ""), quote=True)


def render(report, idea_title=""):
    """HTML for the 'what this is' block shown above the raw report."""
    e = explain(report, idea_title)
    if not e["words"]:
        return ""

    cast_html = ""
    if e["cast"]:
        chips = "".join(
            f'<span class="cast-chip">{_esc(n)}</span>' for n in e["cast"][:10])
        cast_html = (
            '<div class="sim-row"><div class="sim-k">Who took part</div>'
            f'<div class="sim-v">{chips}'
            '<div class="sim-note">These are simulated personas, not real companies '
            'or people. MiroFish named them from your idea and had them react.</div>'
            '</div></div>')

    scen_html = ""
    if e["scenario"]:
        items = "".join(f"<li>{_esc(s)}</li>" for s in e["scenario"])
        scen_html = (
            '<div class="sim-row"><div class="sim-k">What was announced</div>'
            f'<div class="sim-v"><ul class="sim-list">{items}</ul></div></div>')

    head_html = ""
    if e["headline"]:
        head_html = (
            '<div class="sim-headline">'
            f'<span class="sim-q">In one line</span>{_esc(e["headline"])}</div>')

    sections_html = ""
    if e["sections"]:
        items = "".join(f"<li>{_esc(s)}</li>" for s in e["sections"])
        sections_html = (
            '<div class="sim-row"><div class="sim-k">How the report is built</div>'
            f'<div class="sim-v"><ol class="sim-list">{items}</ol></div></div>')

    warn = ""
    if e["uses_forecast_language"]:
        warn = (
            '<div class="sim-warn">⚠ <b>The report reads like a forecast, but it is not one.</b> '
            'It says things like “X <i>will</i> do Y”. MiroFish ran personas on a simulated '
            'graph and recorded what they did in that run. It did not measure real buyers, '
            'and no time-series model was involved.</div>')

    return f"""
<div class="sim-intro">
  <div class="sim-title">🔍 What this report is</div>
  <p>A <b>reaction simulation</b>, not a prediction. MiroFish took your idea, turned it into
  a small social graph of named participants, and let LLM personas react to the announcement
  inside that graph. What you are reading is <b>what those personas did in one run</b>.</p>

  <div class="sim-facts">
    <div class="sim-fact"><b>{e['words']:,}</b><span>words</span></div>
    <div class="sim-fact"><b>{len(e['cast'])}</b><span>participants</span></div>
    <div class="sim-fact"><b>1</b><span>run (not an average)</span></div>
    <div class="sim-fact no"><b>No</b><span>TimesFM used</span></div>
  </div>

  {head_html}
  {scen_html}
  {cast_html}
  {sections_html}
  {warn}

  <div class="sim-row"><div class="sim-k">How much to trust it</div>
  <div class="sim-v">
    <div class="sim-trust">
      <div class="tr-good">✓ Useful for</div>
      <ul class="sim-list">
        <li>Hearing objections you had not thought of</li>
        <li>Seeing which competitor gets threatened, and how</li>
        <li>Picking the wording of a pitch before you send it</li>
      </ul>
      <div class="tr-bad">✗ Not evidence of</div>
      <ul class="sim-list">
        <li>That anyone will actually pay</li>
        <li>Market size, growth, or timing (that needs real data, not a simulation)</li>
        <li>What a specific real company will do next</li>
      </ul>
    </div>
  </div></div>
</div>
<style>
.sim-intro{{background:#0d1a22;border:1px solid #24404f;border-left:4px solid #38bdf8;
  border-radius:12px;padding:16px 18px;margin:0 0 26px;font-size:14.5px;line-height:1.65}}
.sim-title{{font-size:16px;font-weight:800;color:#7dd3fc;margin-bottom:8px}}
.sim-intro p{{color:#c9d8e0;margin:0 0 12px}}
.sim-intro b{{color:#e9edef}}
.sim-facts{{display:flex;gap:9px;flex-wrap:wrap;margin:0 0 14px}}
.sim-fact{{background:#111b21;border:1px solid #1d2a33;border-radius:10px;
  padding:8px 13px;min-width:88px}}
.sim-fact b{{display:block;font-size:17px;color:#4ade80;line-height:1.2}}
.sim-fact.no b{{color:#94a3b8;font-size:14px}}
.sim-fact span{{color:#667781;font-size:11px;text-transform:uppercase;letter-spacing:.4px}}
.sim-headline{{background:#0a1f16;border-left:3px solid #10b981;border-radius:7px;
  padding:10px 13px;margin:0 0 14px;color:#d1fae5;font-size:14px}}
.sim-q{{display:block;color:#4ade80;font-size:10.5px;text-transform:uppercase;
  letter-spacing:.7px;font-weight:700;margin-bottom:4px}}
.sim-row{{display:flex;gap:14px;padding:11px 0;border-top:1px solid #1a2c36;flex-wrap:wrap}}
.sim-k{{flex:0 0 132px;color:#7dd3fc;font-size:11.5px;text-transform:uppercase;
  letter-spacing:.6px;font-weight:700;padding-top:2px}}
.sim-v{{flex:1;min-width:220px;color:#c9d8e0}}
.sim-list{{margin:0;padding-left:19px}}
.sim-list li{{margin:4px 0}}
.sim-note{{color:#667781;font-size:12px;margin-top:7px;font-style:italic}}
.cast-chip{{display:inline-block;background:#13232c;border:1px solid #24404f;color:#a5d8f3;
  border-radius:11px;padding:3px 10px;margin:0 5px 5px 0;font-size:12.5px;font-weight:600}}
.sim-warn{{background:#2a1d05;border:1px solid #713f12;color:#fde68a;border-radius:9px;
  padding:11px 14px;margin:12px 0;font-size:13.5px}}
.sim-warn b{{color:#fbbf24}}
.sim-trust{{display:flex;gap:20px;flex-wrap:wrap}}
.sim-trust>div{{flex:1;min-width:190px}}
.tr-good{{color:#4ade80;font-size:11.5px;font-weight:800;text-transform:uppercase;
  letter-spacing:.5px;margin-bottom:5px}}
.tr-bad{{color:#f87171;font-size:11.5px;font-weight:800;text-transform:uppercase;
  letter-spacing:.5px;margin:10px 0 5px}}
@media(max-width:640px){{
  .sim-k{{flex:0 0 100%}}
  .sim-intro{{padding:13px 14px;font-size:14px}}
}}
</style>"""
