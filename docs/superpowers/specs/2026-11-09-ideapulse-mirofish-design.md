# IdeaPulse → MiroFish reaction simulation

*Design approved 2026-11-09. Scope: on-demand only. TimesFM is explicitly out of scope — see §8.*

---

## 1. Problem

Today every idea in IdeaPulse is judged by a single LLM call (`deep_eval` → `evaluations/eval_<id>.md`). That produces one opinion from one model, with no exploration of how different kinds of people would react differently.

MiroFish runs LLM personas on simulated social platforms. Given an idea as seed material, it produces a report on how buyers, competitors, and critics plausibly respond — including disagreement between segments, which a single-shot evaluation cannot show.

## 2. Goal

One click on an idea produces a MiroFish reaction report, saved beside the existing deep evaluation, without leaving the IdeaPulse UI and without operator intervention.

Non-goals: batch evaluation of the backlog; replacing `deep_eval`; changing the idea-fetching pipeline; TimesFM forecasting (no viable data — §8).

## 3. Architecture

A new module owns all MiroFish interaction. `app.py` gains a route and a view, and stays a thin caller — matching how `deepeval` already delegates to `deep_eval` and `jobs`.

```
idea_pulse/
  mirofish_bridge.py     # new: seed building, preflight, stage orchestration, report fetch
  app.py                 # + /simulate/{id}, + /simulation/{id}, + sim_path column
  evaluations/sim_<id>.md  # output artifact
```

`mirofish_bridge.py` shells out to the `foresight` skill CLI
(`~/.hermes/skills/mlops/foresight/scripts/mirofish.py`) and parses its JSON envelope.

### Why a subprocess rather than importing or re-implementing

The skill CLI already encodes four non-obvious fixes: one shared graph handle per process (Kuzu allows a single handle per file), explicit `close-env` after simulation (otherwise `runner_status` stays `running` and the report API returns 409 forever), the Kuzu `RelatesToNode_` edge query (edges are intermediate nodes, not relationships), and ontology-derived entity types (without them MiroFish's entity filter drops every node and preparation fails with "no matching entities").

Re-implementing the polling against MiroFish's HTTP API would re-introduce those bugs in a second place. Importing the module would need `sys.path` surgery for a CLI-shaped file. Subprocess overhead is negligible against a ~6-minute run.

## 4. Data flow

1. User clicks Simulate on an idea → `POST /simulate/{id}?async_=1`.
2. Route starts a `jobs.start` job (2 steps) — identical to `deepeval`'s pattern.
3. Bridge writes a seed file to a temp path.
4. Bridge runs, in order: `health` → `ontology` → `build` → `simulate` → `report --out`.
5. Report markdown saved to `evaluations/sim_<id>.md`; `items.sim_path` updated.
6. The page polls `/jobs/<id>`; the progress label shows the current stage.

### Seed construction

Raw IdeaPulse rows are too thin to simulate. Measured on idea 7139 (a real
r/EntrepreneurRideAlong post):

| Seed | Graph nodes | Sim actions | Report size |
|---|---|---|---|
| title + `meta` (719 chars) | **2** | 10 | 6,540 chars |
| enriched with named market participants | **6** | 36 | 29,874 chars |

The raw seed has no named actors. MiroFish simulates *entities in the graph*, so
with only "Apollo" and a generic "Entrepreneur" node it produced two personas —
one of whom was the vendor talking to itself. The report then honestly documented
that emptiness ("market silence", "only one agent exists"), which is a correct
reading of an unusable simulation.

The bridge therefore **enriches the seed before simulating**. A short LLM call
(reusing IdeaPulse's existing `llm.py` free-model chain) expands the idea into:

- the problem statement, in the user's own terms
- the named incumbent / competitor / substitute products
- the named buyer personas and their roles
- likely critics (analysts, procurement, technical reviewers)

This is a seed-expansion step, not an evaluation — it must only surface facts
implied by the idea, and the prompt forbids inventing company names for
unidentified parties.

Seed layout sent to MiroFish:

```
title
meta
source
[expand_idea(title, meta) -> named problem, competitors, buyers, critics]
[evaluation excerpt when evaluations/eval_<id>.md exists]
```

The simulation requirement is fixed:
*"How would prospective buyers, competitors, and critics react to this idea being announced?"*

### Language must be set explicitly

MiroFish chooses its output language from `Accept-Language` and **silently falls
back to Chinese (`zh`)** when the header is absent. Its background threads have no
request context, so `get_locale()` returns the `'zh'` default and every report
came back in Chinese regardless of the seed language — measured at 95% CJK on the
first test.

The bridge always sends `Accept-Language: en`. With the header the same pipeline
returned 100% English. `mirofish.py` gained a `--lang` flag (default `en`) so this
is settable rather than accidental.

### Progress mapping

| Stage | Progress bar |
|---|---|
| ontology | 0–30% |
| build | 30–55% |
| simulate | 55–85% |
| report | 85–100% |

These come from real stage boundaries in the CLI output, not a timer.

### Report completeness check is separate from `_report_complete`

`app.py:883` already has `_report_complete()`, but it is specific to the deep-eval format: it requires a `## Final call` heading. MiroFish reports use a different structure (sections like "核心事件" / event analysis), so reusing that helper would mark every successful simulation as incomplete.

`mirofish_bridge.py` gets its own check — non-empty, longer than 200 characters, and not ending mid-sentence — and the two helpers stay independent so changing one does not silently break the other.

### Targeted cleanup in `app.py`

`deep_eval` is defined twice (`app.py:515` and `app.py:517`). The first is a two-line dead stub shadowed immediately by the real definition, and it opens a sqlite connection at module level for no reason. Confirmed via AST scan: `deep_eval` is the only duplicate top-level definition in the file.

This is removed as part of the change: the new `/simulate` route copies the async-job pattern from this exact function, so leaving a confusing duplicate in the reference implementation invites the same mistake twice.

## 5. Cost and caching

A run costs roughly 6–11 minutes and a few cents of OpenRouter usage. Therefore:

- **On-demand only.** The backlog is 4,420 items; batching is not viable.
- **Default `--max-rounds 12`.** Main cost lever.
- **Reuse.** If `sim_<id>.md` exists and is non-trivial, return `existing` unless `force=1` — mirroring `deep_eval`.
- One simulation per click; no queueing of multiple ideas.

Measured wall-clock on the real idea (M-series Mac, local MLX graph):

| Stage | Time |
|---|---|
| seed expansion (LLM) | ~10s |
| ontology | 60–200s |
| build | 40–200s |
| simulate (12 rounds) | 180–230s |
| report | 320–400s |

### Quality gate

A simulation with too few entities produces a report that merely narrates its own
emptiness. The bridge refuses to run when seed expansion yields fewer than three
named actors, returning a clear error rather than spending minutes to produce a
hollow report. The test above is the regression case: two nodes produced a
6.5k-char report that said nothing.

## 6. Error handling

| Failure | Behaviour |
|---|---|
| MiroFish not running | Attempt one `mirofish_server.sh start`; if health still fails, error names that command |
| Shim not installed (Zep 401) | Detect in output; error points to `install_local_graph.py` |
| Stage failure in envelope (`ok: false`) | Surface the CLI's `error` field verbatim; job ends in `error`, red bar |
| Report truncated / empty | Do not write `sim_path`; treat as failure |
| Whole run exceeds 20 min | Timeout; report partials not cached |

No silent fallbacks: a failed simulation must never look like a successful one. The existing `deepeval` job already raises so the bar goes red — reuse that.

## 7. Testing

Unit tests, no network or model:

- `build_seed()`: missing `meta`, HTML entities in titles, very long titles, absent evaluation file, present evaluation file.
- `expand_idea()` parsing: valid response, malformed JSON (must fall back to the raw seed and report low confidence), and a response naming fewer than three actors (must trigger the quality gate).
- Stage→progress mapping for all four stages.
- Envelope parsing: success, `ok: false` with error text, malformed JSON.
- Report completeness: a MiroFish-shaped report with no `## Final call` heading must pass (guards against reusing `_report_complete`), and a truncated one must fail.
- Language: the client sends `Accept-Language` (regression for the 95%-Chinese bug).
- Preflight: server-down path produces the actionable message.

Integration checks, run manually and recorded here:

| Check | Result 2026-11-09 |
|---|---|
| Raw seed (2 nodes) | 10 actions, 6,540-char report, narrated emptiness → **quality gate rejected** |
| Enriched seed (6 nodes) | 36 actions, 29,874-char English report with concrete objections (vendor lock-in, contract overlap) |
| Language | 5% English before `Accept-Language`, 100% after |

## 8. TimesFM: why excluded

Verified against the live database on 2026-11-09:

| Check | Result |
|---|---|
| `Google Trends IN` rows | 501 |
| Distinct keywords | 501 |
| Keywords appearing on more than one day | **0** |
| Any title in the whole DB on more than one day | 9 of 4,420 |
| Daily item counts | 17, 21, 20, 19, 19, 18 — scraper cap |

The Trends source records *what was trending on a given day*, never a keyword's history, so no per-keyword series exists. Daily counts are bounded by the scraper, so forecasting them models the scraper, not the market. Feeding TimesFM this data would emit confident numbers with no signal behind them.

A real TimesFM integration requires IdeaPulse to first record historical interest per keyword (Google Trends exposes this), building genuine series over time. That is a data-collection project, deliberately deferred.

## 9. Out of scope

- Batch/backlog simulation
- Automated reactions inside `deep_eval`
- TimesFM or any forecasting
- Changes to idea fetching or the existing evaluator
- Exposing MiroFish beyond localhost
