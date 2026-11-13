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
    # money and metric words: "**Earnings**" was listed as a participant on a real run
    "earnings", "revenue", "income", "profit", "profits", "sales", "installs",
    "downloads", "users", "costs", "cost", "pricing", "margin", "margins", "valuation",
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


# ---------------------------------------------------------------- honesty checks
#
# Two questions a reader cannot answer from the report itself:
#   1. Did the run stay on the idea? Measured on real runs: the "Good for Bots"
#      report (an AI-crawler URL scanner) named 0 of the idea's 8 key terms and
#      was about Clay, Apollo and ZoomInfo instead. The DMARC run named 7 of 10.
#   2. Did it have enough to go on? The Android-app run quoted 5 facts and said
#      itself that it had "no first-person statements".

# Words too general to show a report is about *this* idea.
_TOPIC_STOP = set("""
a an the and or of for to in on with without that this these those is are be by from as at it its
your their our you can could would will into about over under than then when what which who whom
whose how why simple free tool tools app apps site sites website websites report reports market
product products service services new based using use users user people business businesses
small help helps check checks idea ideas announce announced get gets make makes more most very
just only also like via per whether good last month brought specific growing fast quietly
costing grows grew online page pages first best really
""".split())

_FACT = re.compile(r'^>\s*"(.+?)"\s*$', re.M)
_ADMITS_THIN = re.compile(
    r"no first-person statements|evidence-poor|only (?:one|two|three|a few) (?:current )?facts"
    r"|limited (?:data|evidence)|offers only a few signals", re.I)

THIN_FACTS = 6          # fewer quoted facts than this and the run is a sketch
ON_TOPIC_SHARE = 0.4    # share of the idea's key terms the report must use twice or more
OFF_TOPIC_SHARE = 0.2


def key_terms(text, limit=12):
    """Distinctive words of the idea, in order: 'dmarc', 'outlook.com', 'crawlers'."""
    out = []
    for w in re.findall(r"[a-z][a-z0-9.\-]{2,}", (text or "").lower()):
        w = w.strip(".-")
        if len(w) >= 4 and w not in _TOPIC_STOP and w not in out:
            out.append(w)
    return out[:limit]


def topic_check(report, idea_text):
    """Does the report keep talking about the idea it was given?"""
    terms = key_terms(idea_text)
    low = (report or "").lower()
    counts = {t: len(re.findall(r"\b" + re.escape(t), low)) for t in terms}
    found = [t for t in terms if counts[t] >= 2]
    missing = [t for t in terms if counts[t] == 0]
    if not terms:
        level = "unknown"
    else:
        share = len(found) / float(len(terms))
        level = "on" if share >= ON_TOPIC_SHARE else ("off" if share < OFF_TOPIC_SHARE else "partial")
    return {"level": level, "terms": terms, "found": found, "missing": missing}


def thin_check(report):
    """How many distinct facts the run quoted, and whether it admits it had little."""
    facts = len(set(_FACT.findall(report or "")))
    admits = bool(_ADMITS_THIN.search(report or ""))
    return {"facts": facts, "admits": admits, "thin": facts < THIN_FACTS or admits}


def _and(words, joiner="and"):
    words = list(words)
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + f" {joiner} " + words[-1]


def run_verdict(report, idea_text):
    """One plain verdict per run: usable, off_topic or thin, with a sentence saying why."""
    topic = topic_check(report, idea_text)
    thin = thin_check(report)
    if topic["level"] == "off":
        names = _and(extract_cast(report, limit=3)) or "other things"
        missing = _and(topic["missing"][:3], "or")
        return {"kind": "off_topic", "topic": topic, "thin": thin,
                "label": "Went off topic",
                "say": (f"The report is mostly about {names} and never mentions {missing}. "
                        "It answered a different question; don't use it for this idea.")}
    if thin["thin"]:
        own = " and says so itself" if thin["admits"] else ""
        return {"kind": "thin", "topic": topic, "thin": thin,
                "label": "Too little to go on",
                "say": (f"The run had only {thin['facts']} fact{'' if thin['facts'] == 1 else 's'} "
                        f"to work with{own}. Treat it as a rough sketch.")}
    partly = "Mostly on topic. " if topic["level"] == "partial" else ""
    return {"kind": "usable", "topic": topic, "thin": thin,
            "label": "Stayed on the idea",
            "say": (f"{partly}It quoted {thin['facts']} facts to work from. "
                    "Read the quotes for objections, rivals and where buyers gather.")}


def example_quotes(report, idea_text, limit=3):
    """Quoted facts that use the idea's own words: the most useful lines in a report."""
    terms = topic_check(report, idea_text)["found"]
    out = []
    for q in dict.fromkeys(_FACT.findall(report or "")):
        if not terms or any(t in q.lower() for t in terms):
            out.append(q)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- rendering

def _esc(s):
    import html
    return html.escape(str(s or ""), quote=True)


def render(report, idea_title="", idea_what=""):
    """HTML for the plain-language block shown above the raw report."""
    e = explain(report, idea_title)
    if not e["words"]:
        return ""
    v = run_verdict(report, idea_what or idea_title)

    scen_html = ""
    if e["scenario"]:
        items = "".join(f"<li>{_esc(s)}</li>" for s in e["scenario"])
        scen_html = f'<h3>What the personas were told</h3><ul>{items}</ul>'

    cast_html = ""
    if e["cast"]:
        cast_html = (
            f'<h3>Who took part</h3><p>{_esc(", ".join(e["cast"][:10]))}.</p>'
            '<p class="sx-note">These are simulated personas, not real companies or people. '
            'MiroFish named them from your idea and had them react.</p>')

    warn = ""
    if e["uses_forecast_language"]:
        warn = ('<p class="sx-warn">The report below says things like “X <i>will</i> do Y”. '
                'Read that as “in this one role-play, X did Y”. Nobody measured real buyers.</p>')

    n = len(e["cast"])
    who = f", {n} named participant{'' if n == 1 else 's'}" if n else ""
    return f"""
<section class="sx">
  <p class="sx-lede">This is a rehearsal, not a forecast.</p>
  <p>MiroFish gave AI personas your idea and let them act out how buyers, rivals and critics
  might react. Below is what they did in one run ({e['words']:,} words{who}).
  No TimesFM or other forecasting model was used, and no real person was asked.
  <a href="/simulations">How simulations work, with real examples</a></p>

  <div class="sx-check {v['kind']}">
    <p class="sx-label">{_esc(v['label'])}</p>
    <p>{_esc(v['say'])}</p>
  </div>

  {scen_html}
  {cast_html}
  {warn}

  <div class="sx-trust">
    <div><h3>Useful for</h3><ul>
      <li>Hearing objections you had not thought of</li>
      <li>Spotting which rival feels threatened</li>
      <li>Finding where buyers gather, to pitch there</li></ul></div>
    <div><h3>Not evidence of</h3><ul>
      <li>That anyone will pay</li>
      <li>Market size, growth or timing</li>
      <li>What a real company will do next</li></ul></div>
  </div>
</section>
<style>
.sx{{max-width:66ch;margin:0 0 34px;padding:0 0 26px;border-bottom:1px solid #25313b;
  color:#e8e2d4;font:16px/1.6 'Atkinson Hyperlegible',-apple-system,'Segoe UI',sans-serif}}
.sx p{{margin:0 0 12px}}
.sx a{{color:#e8e2d4}}
.sx-lede{{font:400 26px/1.3 'Newsreader',Georgia,serif;margin-bottom:10px!important}}
.sx h3{{font-size:14px;font-weight:700;color:#8b979f;margin:18px 0 6px}}
.sx ul{{margin:0;padding-left:20px}} .sx li{{margin:3px 0}}
.sx-note{{color:#8b979f;font-size:14px}}
.sx-check{{border-left:3px solid #8b979f;padding:4px 0 4px 14px;margin:18px 0}}
.sx-check p{{margin:0 0 4px}}
.sx-label{{font-weight:700}}
.sx-check.usable{{border-color:#f0c64a}} .sx-check.usable .sx-label{{color:#f0c64a}}
.sx-check.thin{{border-color:#7fa8d6}} .sx-check.thin .sx-label{{color:#7fa8d6}}
.sx-check.off_topic{{border-color:#e48a78}} .sx-check.off_topic .sx-label{{color:#e48a78}}
.sx-warn{{color:#8b979f;font-size:14.5px}}
.sx-trust{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
@media(max-width:640px){{.sx-trust{{grid-template-columns:1fr}} .sx-lede{{font-size:22px}}}}
</style>"""
