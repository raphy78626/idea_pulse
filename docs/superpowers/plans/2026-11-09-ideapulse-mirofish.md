# IdeaPulse → MiroFish Reaction Simulation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an on-demand "Simulate" action to IdeaPulse that runs a MiroFish multi-agent reaction simulation for one idea and saves the resulting report beside the existing deep evaluation.

**Architecture:** A new module `mirofish_bridge.py` owns every MiroFish interaction and shells out to the `foresight` skill CLI (`~/.hermes/skills/mlops/foresight/scripts/mirofish.py`), parsing its JSON envelopes. `app.py` gains one route and one view and stays a thin caller, matching how `deepeval` delegates to `deep_eval` + `jobs`.

**Tech Stack:** Python 3.9 (the live interpreter), FastAPI, sqlite3, the `jobs` thread pool already in the repo, and the foresight skill's CLI. Tests: pytest 8.4.2 via `/usr/bin/python3`.

---

## CRITICAL CONSTRAINTS (read before any task)

1. **The live app runs on `/usr/bin/python3` = Python 3.9.6.** Verified: `/usr/bin/python3 -V` → `Python 3.9.6`. Do **not** use `str | None`, `list[str]`, `match`, or any 3.10+ syntax anywhere in `app.py`, `mirofish_bridge.py`, or the tests. Use `Optional[str]`, `List[str]`, `Dict[str, str]` from `typing`.
2. **Run tests with `/usr/bin/python3 -m pytest`** (pytest 8.4.2 is installed there). `pytest` is NOT installed in the `demo_bot` venv, and that venv's LaunchAgent is not the loaded one.
3. **`app.py` is 1319 lines** — do not restructure it. Add only what the plan specifies.
4. **Never write to `~/.hermes/` from tests.** Use `tmp_path`.
5. Every subprocess call to the skill CLI must pass `--lang en`; MiroFish otherwise returns Chinese (measured 95% CJK).

## File Structure

| File | Responsibility |
|---|---|
| `mirofish_bridge.py` (create) | Seed building, LLM seed expansion, quality gate, CLI invocation, envelope parsing, report completeness. Knows nothing about FastAPI or the DB. |
| `app.py` (modify) | `sim_path` column, `/simulate/{id}`, `/simulation/{id}`, Simulate button, and removal of the dead `deep_eval` stub. |
| `tests/test_mirofish_bridge.py` (create) | Unit tests for the bridge — no network, no model, no subprocess. |
| `tests/test_sim_routes.py` (create) | Route-level tests with the bridge and jobs stubbed. |

The bridge is deliberately import-light: it must be importable in tests without touching OpenRouter or MiroFish.

---

### Task 1: Bridge skeleton — report completeness

**Files:**
- Create: `mirofish_bridge.py`
- Test: `tests/test_mirofish_bridge.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mirofish_bridge'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Bridge from IdeaPulse to the MiroFish reaction simulator.

Talks to the foresight skill CLI rather than MiroFish's HTTP API directly, so the
pipeline's hard-won fixes (one shared graph handle per process, explicit
close-env, Kuzu RelatesToNode_ edges, ontology-derived entity types) live in one
place instead of being duplicated here.

Python 3.9: no PEP 604 unions, no builtin generic aliases.
"""
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MIN_REPORT_CHARS = 200


def report_is_complete(markdown):
    # type: (str) -> bool
    """A MiroFish report is complete when it is substantial and ends a sentence.

    Deliberately separate from app._report_complete, which requires a
    '## Final call' heading that only deep-eval reports have.
    """
    if not markdown or len(markdown.strip()) < MIN_REPORT_CHARS:
        return False
    return bool(re.search(r"[.!?\"')\]]\s*$", markdown.rstrip()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add mirofish_bridge.py tests/test_mirofish_bridge.py
git commit -m "feat(sim): report completeness check for MiroFish output"
```

---

### Task 2: Seed building

**Files:**
- Modify: `mirofish_bridge.py`
- Test: `tests/test_mirofish_bridge.py`

- [ ] **Step 1: Write the failing test**

```python
import mirofish_bridge


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


def test_seed_includes_evaluation_excerpt(tmp_path):
    seed = mirofish_bridge.build_seed("T", "m", "Reddit", evaluation="Verdict: SKIP 2/10")
    assert "SKIP 2/10" in seed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v -k seed`
Expected: FAIL with `AttributeError: module 'mirofish_bridge' has no attribute 'build_seed'`

- [ ] **Step 3: Write minimal implementation**

```python
MAX_TITLE_CHARS = 400
MAX_META_CHARS = 1200


def _clean(text, limit):
    # type: (Optional[str], int) -> str
    if not text:
        return ""
    # feedparser/Reddit feeds double-escape; unescape twice so &amp;quot; resolves
    out = html.unescape(html.unescape(str(text))).strip()
    return out[:limit]


def build_seed(title, meta, source, expansion="", evaluation=""):
    # type: (str, Optional[str], str, str, str) -> str
    """Assemble the seed document MiroFish reads.

    `expansion` names the market participants (see expand_idea); without named
    actors MiroFish builds a two-node graph and the report only narrates its own
    emptiness -- measured at 2 nodes / 10 actions versus 6 nodes / 36 actions.
    """
    parts = [
        "Idea: " + _clean(title, MAX_TITLE_CHARS),
        "",
        "Source: " + _clean(source, 200),
        "",
        "Context:",
        _clean(meta, MAX_META_CHARS) or "(no further context was provided with this idea)",
    ]
    if expansion.strip():
        parts += ["", "Market participants:", expansion.strip()]
    if evaluation.strip():
        parts += ["", "Prior evaluation of this idea:", evaluation.strip()[:1500]]
    return "\n".join(parts) + "\n"
```

Add `import html` to the module imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v -k seed`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add mirofish_bridge.py tests/test_mirofish_bridge.py
git commit -m "feat(sim): build simulation seed from idea record"
```

---

### Task 3: Seed expansion and the quality gate

**Files:**
- Modify: `mirofish_bridge.py`
- Test: `tests/test_mirofish_bridge.py`

- [ ] **Step 1: Write the failing test**

```python
import mirofish_bridge


GOOD_EXPANSION = """Problem: mid-market RevOps teams rebuild prospect lists every campaign.
Apollo.io - incumbent contact-data vendor.
Clay - newer enrichment workflow tool.
ZoomInfo - enterprise contact provider.
Mid-market RevOps managers - the buyer persona."""


def test_expand_idea_returns_text(monkeypatch):
    monkeypatch.setattr(mirofish_bridge, "_llm_chat", lambda s, u, **k: (GOOD_EXPANSION, "test-model"))
    text, model = mirofish_bridge.expand_idea("list building idea", "context")
    assert "Apollo.io" in text
    assert model == "test-model"


def test_expand_idea_falls_back_to_empty_on_llm_failure(monkeypatch):
    monkeypatch.setattr(mirofish_bridge, "_llm_chat", lambda s, u, **k: ("", "no model"))
    text, model = mirofish_bridge.expand_idea("t", "m")
    assert text == ""
    assert "no model" in model


def test_count_named_actors_counts_only_capitalised_entries():
    assert mirofish_bridge.count_named_actors(GOOD_EXPANSION) >= 3


def test_count_named_actors_ignores_prose_only():
    prose = "the problem is that teams spend a week rebuilding lists every campaign"
    assert mirofish_bridge.count_named_actors(prose) == 0


def test_quality_gate_passes_with_enough_actors():
    ok, reason = mirofish_bridge.check_seed_quality(GOOD_EXPANSION)
    assert ok is True
    assert reason == ""


def test_quality_gate_rejects_thin_seed():
    ok, reason = mirofish_bridge.check_seed_quality("just some prose about a problem")
    assert ok is False
    assert "named" in reason.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v -k "expand or actor or quality"`
Expected: FAIL with `AttributeError: module 'mirofish_bridge' has no attribute 'expand_idea'`

- [ ] **Step 3: Write minimal implementation**

```python
MIN_NAMED_ACTORS = 3

EXPAND_SYSTEM = """You expand a raw business-idea record into a market map for a simulator.

Output plain text, no markdown, no JSON. Use exactly these sections:

Problem: one or two sentences, the pain in the poster's own terms.
Market participants:
One line per participant, formatted "Name - role and relevance".
List 6 to 9 participants covering all four kinds: the incumbents or substitutes
customers already use, competing products, the buyer personas, and likely critics
(analysts, procurement, technical reviewers).

Rules:
- Only name companies and products that the idea text implies or that plainly
  operate in that market. If you cannot name a real one, describe the role
  ("the incumbent CRM vendor") instead of inventing a company.
- Never invent facts about pricing, funding, or market share.
- Do not evaluate or recommend. Map the market only."""

# Capitalised multiword or dotted names on a dash-bulleted line, e.g. "Apollo.io - ..."
_ACTOR_LINE = re.compile(r"^\s*([A-Z][\w.'&-]*(?:\s+[A-Z][\w.'&-]*)*)\s*[-–—]", re.M)


def count_named_actors(text):
    # type: (str) -> int
    if not text:
        return 0
    return len(set(m.group(1).strip() for m in _ACTOR_LINE.finditer(text)))


def expand_idea(title, meta):
    # type: (str, str) -> Tuple[str, str]
    """Ask the free-model chain to name the market. Returns (text, model)."""
    prompt = "Idea: %s\nContext: %s" % (title, (meta or "")[:800])
    text, model = _llm_chat(EXPAND_SYSTEM, prompt, max_tokens=900, timeout=90)
    return (text or "", model)


def check_seed_quality(expansion):
    # type: (str) -> Tuple[bool, str]
    """Refuse to spend minutes on a simulation with too few actors to interact."""
    actors = count_named_actors(expansion)
    if actors < MIN_NAMED_ACTORS:
        return False, (
            "only %d named market participant(s) found; at least %d are needed "
            "for a meaningful simulation" % (actors, MIN_NAMED_ACTORS)
        )
    return True, ""


def _llm_chat(system, user, **kwargs):
    # imported lazily so tests never need OpenRouter or the llm module's state
    import llm
    return llm.chat(system, user, **kwargs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v -k "expand or actor or quality"`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add mirofish_bridge.py tests/test_mirofish_bridge.py
git commit -m "feat(sim): LLM seed expansion plus minimum-actor quality gate"
```

---

### Task 4: Skill CLI invocation and envelope parsing

**Files:**
- Modify: `mirofish_bridge.py`
- Test: `tests/test_mirofish_bridge.py`

- [ ] **Step 1: Write the failing test**

```python
import json
import mirofish_bridge


def test_cli_path_points_at_foresight_skill():
    p = mirofish_bridge.cli_path()
    assert p.name == "mirofish.py"
    assert "foresight" in str(p)


def test_parse_envelope_reads_success():
    out = json.dumps({"ok": True, "project_id": "proj_1"})
    data = mirofish_bridge.parse_envelope(out)
    assert data["project_id"] == "proj_1"


def test_parse_envelope_raises_on_ok_false():
    out = json.dumps({"ok": False, "error": "cannot reach MiroFish at http://x"})
    try:
        mirofish_bridge.parse_envelope(out)
    except mirofish_bridge.BridgeError as e:
        assert "cannot reach MiroFish" in str(e)
    else:
        raise AssertionError("expected BridgeError")


def test_parse_envelope_raises_on_garbage():
    try:
        mirofish_bridge.parse_envelope("not json at all")
    except mirofish_bridge.BridgeError as e:
        assert "unreadable" in str(e).lower()
    else:
        raise AssertionError("expected BridgeError")


def test_parse_envelope_ignores_leading_stderr_noise():
    out = "some warning line\n" + json.dumps({"ok": True, "characters": 5})
    assert mirofish_bridge.parse_envelope(out)["characters"] == 5


def test_run_stage_always_passes_lang_en(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd

        class R:
            returncode = 0
            stdout = json.dumps({"ok": True})
            stderr = ""
        return R()

    monkeypatch.setattr(mirofish_bridge.subprocess, "run", fake_run)
    mirofish_bridge.run_stage(["health"])
    assert "--lang" in seen["cmd"]
    assert "en" in seen["cmd"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v -k "cli or envelope or stage"`
Expected: FAIL with `AttributeError: module 'mirofish_bridge' has no attribute 'cli_path'`

- [ ] **Step 3: Write minimal implementation**

```python
SKILL_SCRIPTS = Path.home() / ".hermes" / "skills" / "mlops" / "foresight" / "scripts"
STAGE_TIMEOUT_S = 1500


class BridgeError(RuntimeError):
    """A stage failed in a way the caller must surface, never swallow."""


def cli_path():
    # type: () -> Path
    return SKILL_SCRIPTS / "mirofish.py"


def parse_envelope(text):
    # type: (str) -> Dict[str, object]
    """Parse the CLI's JSON envelope out of stdout that may carry warnings."""
    if not text:
        raise BridgeError("unreadable response from the simulation CLI (empty output)")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise BridgeError("unreadable response from the simulation CLI: %s" % text.strip()[:200])
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        raise BridgeError("unreadable response from the simulation CLI: %s" % text.strip()[:200])
    if not isinstance(data, dict):
        raise BridgeError("unreadable response from the simulation CLI: not an object")
    if not data.get("ok", False):
        raise BridgeError(str(data.get("error") or "simulation stage failed"))
    return data


def run_stage(args, timeout=STAGE_TIMEOUT_S):
    # type: (List[str], int) -> Dict[str, object]
    """Invoke one CLI stage and return its parsed envelope."""
    cmd = [str(cli_path()), "--lang", "en"] + list(args)
    python = os.environ.get("IDEAPULSE_PY") or str(Path.home() / ".hermes/tools/prediction/.venv/bin/python")
    cmd = [python] + cmd
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BridgeError("simulation stage timed out after %ds: %s" % (timeout, " ".join(args)))
    except OSError as exc:
        raise BridgeError("could not start the simulation CLI: %s" % exc)
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        detail = (proc.stderr or "").strip()[-400:]
        raise BridgeError("simulation CLI failed (%d): %s" % (proc.returncode, detail))
    return parse_envelope(proc.stdout or "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v -k "cli or envelope or stage"`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add mirofish_bridge.py tests/test_mirofish_bridge.py
git commit -m "feat(sim): invoke foresight CLI and parse its envelopes"
```

---

### Task 5: Preflight and the full run

**Files:**
- Modify: `mirofish_bridge.py`
- Test: `tests/test_mirofish_bridge.py`

- [ ] **Step 1: Write the failing test**

```python
import mirofish_bridge


def test_preflight_reports_server_down_with_remedy(monkeypatch):
    def boom(args, timeout=0):
        raise mirofish_bridge.BridgeError("cannot reach MiroFish at http://127.0.0.1:5001")
    monkeypatch.setattr(mirofish_bridge, "run_stage", boom)
    ok, hint = mirofish_bridge.preflight()
    assert ok is False
    assert "mirofish_server.sh" in hint


def test_preflight_passes_when_health_ok(monkeypatch):
    monkeypatch.setattr(mirofish_bridge, "run_stage", lambda args, timeout=0: {"ok": True})
    ok, hint = mirofish_bridge.preflight()
    assert ok is True
    assert hint == ""


def test_run_simulation_reports_progress_stages(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, timeout=0):
        calls.append(args[0])
        if args[0] == "ontology":
            return {"ok": True, "project_id": "proj_x"}
        if args[0] == "build":
            return {"ok": True, "graph_id": "g1", "node_count": 6}
        if args[0] == "simulate":
            return {"ok": True, "simulation_id": "sim_x", "rounds_run": 12}
        if args[0] == "report":
            return {"ok": True, "saved_to": str(tmp_path / "r.md"), "characters": 5000}
        return {"ok": True}

    monkeypatch.setattr(mirofish_bridge, "run_stage", fake_run)
    monkeypatch.setattr(mirofish_bridge, "expand_idea", lambda t, m: ("Apollo.io - vendor\nClay - tool\nZoomInfo - vendor", "m"))
    steps = []
    result = mirofish_bridge.run_simulation(
        "title", "meta", "Reddit",
        on_stage=lambda pct, label: steps.append(pct),
        out_path=tmp_path / "r.md")
    assert result["simulation_id"] == "sim_x"
    # health runs first as the preflight check
    assert calls == ["health", "ontology", "build", "simulate", "report"]
    assert steps == sorted(steps), "progress must never go backwards"
    assert steps[-1] == 100


def test_run_simulation_refuses_thin_seed(monkeypatch):
    monkeypatch.setattr(mirofish_bridge, "run_stage", lambda args, timeout=0: {"ok": True})
    monkeypatch.setattr(mirofish_bridge, "expand_idea", lambda t, m: ("prose only, no actors", "m"))
    try:
        mirofish_bridge.run_simulation("t", "m", "Reddit", out_path=None)
    except mirofish_bridge.BridgeError as e:
        assert "named market participant" in str(e)
    else:
        raise AssertionError("expected the quality gate to reject")


def test_run_simulation_surfaces_stage_error(monkeypatch):
    monkeypatch.setattr(mirofish_bridge, "expand_idea", lambda t, m: ("A - x\nB - y\nC - z", "m"))

    def fake_run(args, timeout=0):
        if args[0] == "health":
            return {"ok": True}
        if args[0] == "ontology":
            return {"ok": True, "project_id": "proj_x"}
        raise mirofish_bridge.BridgeError("graph build failed: model returned no JSON")
    monkeypatch.setattr(mirofish_bridge, "run_stage", fake_run)
    try:
        mirofish_bridge.run_simulation("t", "m", "Reddit", out_path=None)
    except mirofish_bridge.BridgeError as e:
        assert "no JSON" in str(e)
    else:
        raise AssertionError("stage errors must propagate")


def test_run_simulation_turns_zep_401_into_install_hint(monkeypatch):
    """A Zep 401 means the local graph shim is missing; the message must say how to fix it."""
    monkeypatch.setattr(
        mirofish_bridge, "run_stage",
        lambda args, timeout=0: (_ for _ in ()).throw(
            mirofish_bridge.BridgeError("status_code: 401, body: unauthorized")))
    try:
        mirofish_bridge.run_simulation("t", "m", "Reddit", out_path=None)
    except mirofish_bridge.BridgeError as e:
        assert "install_local_graph.py" in str(e)
    else:
        raise AssertionError("expected a preflight failure")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v -k "preflight or run_simulation"`
Expected: FAIL with `AttributeError: module 'mirofish_bridge' has no attribute 'preflight'`

- [ ] **Step 3: Write minimal implementation**

```python
STAGES = (
    ("ontology", 0, 30, "mapping the market"),
    ("build", 30, 55, "building the knowledge graph"),
    ("simulate", 55, 85, "running agent reactions"),
    ("report", 85, 100, "writing the report"),
)

SIM_REQUIREMENT = (
    "How would prospective buyers, competitors, and critics react to this idea "
    "being announced?"
)


def preflight():
    # type: () -> Tuple[bool, str]
    """Confirm the MiroFish server is reachable; return a remedy hint on failure."""
    try:
        run_stage(["health"], timeout=30)
    except BridgeError as exc:
        message = str(exc)
        if "cannot reach MiroFish" in message:
            return False, (
                "MiroFish is not running. Start it with "
                "~/.hermes/skills/mlops/foresight/scripts/mirofish_server.sh start"
            )
        if "401" in message or "Zep" in message:
            return False, (
                "MiroFish is running but its local graph backend is not installed. Run "
                "~/.hermes/tools/prediction/.venv/bin/python "
                "~/.hermes/skills/mlops/foresight/scripts/install_local_graph.py"
            )
        return False, message
    return True, ""


def run_simulation(title, meta, source, on_stage=None, out_path=None, evaluation=""):
    # type: (str, Optional[str], str, Optional[object], Optional[Path], str) -> Dict[str, object]
    """Run all MiroFish stages for one idea. Raises BridgeError on any failure."""
    say = on_stage or (lambda pct, label: None)
    emitted = [0]

    def say_once(pct, label):
        # the pre-stage labels (0, 2) overlap the first stage's floor, so guard
        # against the progress bar going backwards
        if pct < emitted[0]:
            return
        emitted[0] = pct
        say(pct, label)

    ok, hint = preflight()
    if not ok:
        raise BridgeError(hint)

    say_once(0, "mapping the market")
    expansion, model = expand_idea(title, meta or "")
    allowed, reason = check_seed_quality(expansion)
    if not allowed:
        raise BridgeError(
            reason + " The idea record is too thin to simulate; add context or name the players."
        )
    say_once(2, "market mapped with %d participants" % count_named_actors(expansion))

    seed = build_seed(title, meta, source, expansion=expansion, evaluation=evaluation)
    workdir = Path(os.environ.get("TMPDIR") or "/tmp") / "ideapulse-sim"
    workdir.mkdir(parents=True, exist_ok=True)
    seed_path = workdir / ("seed-%s.txt" % abs(hash((title or "") + (meta or ""))) )
    seed_path.write_text(seed, encoding="utf-8")

    project_id = ""
    simulation_id = ""
    saved = str(out_path) if out_path else ""

    for stage, lo, hi, label in STAGES:
        say_once(lo, label)
        if stage == "ontology":
            data = run_stage(["ontology", str(seed_path), "--requirement", SIM_REQUIREMENT,
                              "--name", (title or "IdeaPulse idea")[:60]])
            project_id = str(data.get("project_id") or "")
            if not project_id:
                raise BridgeError("ontology stage returned no project id")
        elif stage == "build":
            data = run_stage(["build", "--project-id", project_id])
            if not data.get("graph_id"):
                raise BridgeError("graph build returned no graph id")
        elif stage == "simulate":
            data = run_stage(["simulate", "--project-id", project_id, "--max-rounds", "12"])
            simulation_id = str(data.get("simulation_id") or "")
            if not simulation_id:
                raise BridgeError("simulation stage returned no simulation id")
        else:
            report_args = ["report", "--simulation-id", simulation_id]
            if saved:
                report_args += ["--out", saved]
            data = run_stage(report_args)
            saved = str(data.get("saved_to") or saved)
        say_once(hi, label)

    return {
        "project_id": project_id,
        "simulation_id": simulation_id,
        "report_path": saved,
        "expansion_model": model,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add mirofish_bridge.py tests/test_mirofish_bridge.py
git commit -m "feat(sim): preflight checks and the full four-stage run"
```

---

### Task 6: Read the report and enforce completeness

**Files:**
- Modify: `mirofish_bridge.py`
- Test: `tests/test_mirofish_bridge.py`

- [ ] **Step 1: Write the failing test**

```python
import mirofish_bridge


def test_read_report_returns_text(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("# Report\n\n" + ("Body sentence. " * 30), encoding="utf-8")
    assert "Body sentence" in mirofish_bridge.read_report(str(p))


def test_read_report_returns_empty_for_missing_file(tmp_path):
    assert mirofish_bridge.read_report(str(tmp_path / "nope.md")) == ""


def test_read_report_returns_empty_for_truncated(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("# Report\n\n" + ("Body. " * 40) + "cut mid", encoding="utf-8")
    assert mirofish_bridge.read_report(str(p)) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v -k read_report`
Expected: FAIL with `AttributeError: module 'mirofish_bridge' has no attribute 'read_report'`

- [ ] **Step 3: Write minimal implementation**

```python
def read_report(path):
    # type: (Optional[str]) -> str
    """Return report text, or '' when the file is missing or incomplete.

    An incomplete report must never be cached: app stores sim_path only when this
    returns text, so a truncated run is retried instead of looking finished.
    """
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return text if report_is_complete(text) else ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/usr/bin/python3 -m pytest tests/test_mirofish_bridge.py -v -k read_report`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add mirofish_bridge.py tests/test_mirofish_bridge.py
git commit -m "feat(sim): only cache complete reports"
```

---

### Task 7: `sim_path` column and removal of the dead `deep_eval` stub

**Files:**
- Modify: `app.py:32-36` (column migration list), `app.py:515-517` (dead stub)

- [ ] **Step 1: Add the column to the migration list**

In `app.py`, the `db()` function migrates columns with a list of `"<col> <type>"` strings. Add `sim_path` to that list, next to `report_path`:

```python
                "ttm_days INTEGER", "what TEXT", "reason TEXT", "report_path TEXT", "nutshell TEXT",
                "pitch TEXT", "capital INTEGER", "first_payment TEXT", "earn_verdict TEXT",
                "proof TEXT", "buyer TEXT", "offer_money TEXT", "sim_path TEXT"):
```

- [ ] **Step 2: Verify the migration works on the live database**

Run:
```bash
cd ~/Project/Project_evaluators/idea_pulse && /usr/bin/python3 -c "
import app
c = app.db()
cols = [r[1] for r in c.execute('PRAGMA table_info(items)')]
print('sim_path' in cols)
"
```
Expected: `True`

- [ ] **Step 3: Remove the dead `deep_eval` stub**

At `app.py:515-516` the following two lines shadow the real definition that follows:

```python
def deep_eval(item_id, force=False):
    conn = db(); conn.row_factory = sqlite3.Row
```

Delete exactly those two lines. The real `def deep_eval(item_id, force=False, progress=None):` on the next line becomes the only definition.

- [ ] **Step 4: Verify the app still imports and has one definition**

Run:
```bash
cd ~/Project/Project_evaluators/idea_pulse && /usr/bin/python3 -c "
import ast
tree = ast.parse(open('app.py').read())
names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
print('deep_eval count:', names.count('deep_eval'))
import app; print('import ok')
"
```
Expected:
```
deep_eval count: 1
import ok
```

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "fix(ideapulse): add sim_path column, drop dead duplicate deep_eval stub"
```

---

### Task 8: The `/simulate/{id}` route

**Files:**
- Modify: `app.py` (after the `deepeval` route, around line 795)
- Test: `tests/test_sim_routes.py`

- [ ] **Step 1: Write the failing test**

```python
"""Route tests for /simulate. The bridge is stubbed; no network or model."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    conn = ideapulse.db()
    conn.execute("INSERT INTO items (fetched_on, source, title, url, meta) VALUES (?,?,?,?,?)",
                 ("2026-11-09", "Reddit", "Test idea", "http://x/1", "some context"))
    conn.commit()
    conn.close()
    from fastapi.testclient import TestClient
    return TestClient(ideapulse.app)


def test_simulate_returns_error_when_bridge_rejects(client, monkeypatch):
    def boom(*args, **kwargs):
        raise ideapulse.mirofish_bridge.BridgeError("only 1 named market participant(s) found")
    monkeypatch.setattr(ideapulse.mirofish_bridge, "run_simulation", boom)
    r = client.post("/simulate/1")
    assert r.status_code == 200
    assert "named market participant" in r.text


def test_simulate_404_for_unknown_item(client):
    r = client.post("/simulate/999")
    assert r.status_code == 404


def test_simulate_reuses_existing_report_without_force(client, tmp_path, monkeypatch):
    out = tmp_path / "sim_1.md"
    out.write_text("# Report\n\n" + ("Body sentence. " * 30), encoding="utf-8")
    conn = ideapulse.db()
    conn.execute("UPDATE items SET sim_path=? WHERE id=1", (str(out),))
    conn.commit(); conn.close()
    called = {"n": 0}
    monkeypatch.setattr(ideapulse.mirofish_bridge, "run_simulation",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    r = client.get("/simulate/1")
    assert r.status_code == 200
    assert "existing" in r.json()["status"]
    assert called["n"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/usr/bin/python3 -m pytest tests/test_sim_routes.py -v`
Expected: FAIL — `404` for `/simulate/1` (route does not exist)

- [ ] **Step 3: Write the route**

Add `import mirofish_bridge` to the imports at the top of `app.py`, then add these routes immediately after the `deepeval` function:

```python
@app.post("/simulate/{item_id}")
@app.get("/simulate/{item_id}")
def simulate(item_id: int, force: int = 0, async_: int = 0):
    """Reaction simulation for one idea. async_=1 returns a job id, like deepeval."""
    conn = db(); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "item not found"}

    if not force and row["sim_path"] and mirofish_bridge.read_report(row["sim_path"]):
        return {"ok": True, "status": "existing", "path": row["sim_path"]}

    # local alias: BASE_DIR is defined further down the file (app.py:552)
    base_dir = Path(__file__).parent
    out_path = base_dir / "evaluations" / ("sim_%d.md" % item_id)
    evaluation_md = ""
    if row["report_path"]:
        try:
            evaluation_md = Path(row["report_path"]).read_text()[:1500]
        except OSError:
            evaluation_md = ""

    def work(tick, label):
        def on_stage(pct, msg):
            label(msg)
        result = mirofish_bridge.run_simulation(
            row["title"], row["meta"], row["source"],
            on_stage=on_stage, out_path=out_path, evaluation=evaluation_md)
        text = mirofish_bridge.read_report(result.get("report_path") or str(out_path))
        if not text:
            raise RuntimeError("report was empty or cut off")
        conn2 = db()
        conn2.execute("UPDATE items SET sim_path=? WHERE id=?", (str(out_path), item_id))
        conn2.commit(); conn2.close()
        label("reaction simulation saved")
        return {"path": str(out_path), "simulation_id": result.get("simulation_id")}

    if async_:
        import jobs
        title = (row["title"] or "#%d" % item_id)[:70]
        return {"job": jobs.start("Simulating “%s”" % title, 4, work)}
    result = work(lambda n: None, lambda msg: None)
    return {"ok": True, "status": "new", "path": result["path"]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/usr/bin/python3 -m pytest tests/test_sim_routes.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_sim_routes.py
git commit -m "feat(sim): /simulate route with caching and async jobs"
```

---

### Task 9: The `/simulation/{id}` report view

**Files:**
- Modify: `app.py`
- Test: `tests/test_sim_routes.py`

- [ ] **Step 1: Write the failing test**

Append these two tests to `tests/test_sim_routes.py` (the same file Task 8 created; they use its `client` fixture).

```python
def test_simulation_view_renders_report(client, tmp_path):
    out = tmp_path / "sim_1.md"
    out.write_text("# Report\n\n" + ("Body sentence. " * 30), encoding="utf-8")
    conn = ideapulse.db()
    conn.execute("UPDATE items SET sim_path=? WHERE id=1", (str(out),))
    conn.commit(); conn.close()
    r = client.get("/simulation/1")
    assert r.status_code == 200
    assert "Body sentence" in r.text


def test_simulation_view_404_without_report(client):
    r = client.get("/simulation/1")
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/usr/bin/python3 -m pytest tests/test_sim_routes.py -v -k simulation_view`
Expected: FAIL — 404 for an item that does have a report

- [ ] **Step 3: Write the view**

Add after the existing `/report/{item_id}` route, reusing its rendering exactly:

```python
@app.get("/simulation/{item_id}", response_class=HTMLResponse)
def simulation_report(item_id: int):
    conn = db(); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT sim_path FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    md = mirofish_bridge.read_report(row["sim_path"]) if row and row["sim_path"] else ""
    if not md:
        return HTMLResponse(
            '<p style="font-family:sans-serif;padding:30px">No reaction simulation yet '
            '— run 🎭 simulate first.</p>', status_code=404)
    body = _md_to_html(md)
    return HTMLResponse(REPORT_PAGE.replace("{{BODY}}", body)
                        .replace("{{JOBBAR}}", __import__("jobs").WIDGET))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/usr/bin/python3 -m pytest tests/test_sim_routes.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_sim_routes.py
git commit -m "feat(sim): render the reaction report at /simulation/{id}"
```

---

### Task 10: The Simulate button in the UI

**Files:**
- Modify: `app.py` (the item card template)

- [ ] **Step 1: Find the existing action buttons**

Run: `cd ~/Project/Project_evaluators/idea_pulse && grep -n "deepeval(\|JobBar.watch\|function deepeval" app.py | head -10`

Expected: the `deepeval` button markup and the `async function deepeval(id)` handler at `app.py:1248`. Copy that shape — this page uses `JobBar.watch(job, cb)`, **not** a `JOBS` array.

- [ ] **Step 2: Add the button beside the existing deep-eval action**

Insert next to the deep-eval button, using the same markup style and id interpolation found in Step 1:

```python
<button class="btn" onclick="simulate(<id>)">🎭 Simulate</button>
```

(Replace `<id>` with however the surrounding template interpolates the item id — in an f-string it is `{row['id']}`.)

- [ ] **Step 3: Add the JS handler beside `async function deepeval(id)`**

```javascript
async function simulate(id){
  const s = document.getElementById('status');
  s.textContent = 'simulating reactions (~6-10 min)'; s.style.color = '#667781';
  const r = await (await fetch('/simulate/' + id + '?async_=1')).json();
  if (r.status === 'existing' || r.path && !r.job) {
    s.textContent = 'report already exists — opening';
    s.style.color = '#a78bfa';
    location.href = '/simulation/' + id;
    return;
  }
  if (!r.job) {
    s.textContent = 'failed — ' + (r.error || 'could not start');
    s.style.color = '#f87171';
    return;
  }
  JobBar.watch(r.job, (j) => {
    if (j.state === 'done') {
      s.textContent = 'simulation saved — opening';
      s.style.color = '#a78bfa';
      location.href = '/simulation/' + id;
    } else if (j.state === 'error') {
      s.textContent = 'failed — ' + (j.message || '');
      s.style.color = '#f87171';
    } else {
      s.textContent = j.label || 'simulating…';
    }
  });
}
```

- [ ] **Step 4: Verify the button renders**

Run:
```bash
cd ~/Project/Project_evaluators/idea_pulse && /usr/bin/python3 -c "
import app
html = app.app.__dict__
from fastapi.testclient import TestClient
c = TestClient(app.app)
r = c.get('/')
print('Simulate button present:', 'simulate(' in r.text)
"
```
Expected: `Simulate button present: True`

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "feat(sim): Simulate button on the idea card"
```

---

### Task 11: End-to-end verification on a real idea

**Files:** none (verification only)

- [ ] **Step 1: Confirm MiroFish is up**

Run: `~/.hermes/skills/mlops/foresight/scripts/mirofish_server.sh status`
Expected: `running on http://127.0.0.1:5001` plus a health JSON.

- [ ] **Step 2: Run the bridge directly against a real, actor-rich idea**

Run:
```bash
cd ~/Project/Project_evaluators/idea_pulse && /usr/bin/python3 -c "
import mirofish_bridge, time
t0 = time.time()
r = mirofish_bridge.run_simulation(
    'GTM orchestration platform for mid-market teams',
    'Teams rebuild the prospect list from scratch every campaign, taking about a week; outreach is easy, list building is the bottleneck. Current stack: Apollo exports, spreadsheets, manual enrichment.',
    'Reddit',
    on_stage=lambda pct, msg: print('%3d%%  %s' % (pct, msg)),
    out_path=__import__('pathlib').Path('/tmp/e2e_sim.md'))
print(r)
print('elapsed %.0fs' % (time.time()-t0))
"
```
Expected: four stage lines, a dict with `simulation_id` and `report_path`, elapsed between 300 and 900 seconds.

- [ ] **Step 3: Verify the report is substantive and in English**

Run:
```bash
/usr/bin/python3 -c "
import re
t = open('/tmp/e2e_sim.md').read()
cjk = len(re.findall(r'[\u4e00-\u9fff]', t))
print('chars:', len(t), '| CJK:', cjk)
assert cjk == 0, 'report came back in Chinese - Accept-Language was not sent'
assert len(t) > 5000, 'report too short to be a real simulation'
print('OK')
"
```
Expected: `chars:` above 5000, `CJK: 0`, then `OK`.

- [ ] **Step 4: Run the whole test suite**

Run: `cd ~/Project/Project_evaluators/idea_pulse && /usr/bin/python3 -m pytest tests/ -v`
Expected: all tests pass.

- [ ] **Step 5: Commit any fixes, then record results**

```bash
git add -A && git commit -m "test(sim): end-to-end verification against a real idea" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage**

| Spec requirement | Task |
|---|---|
| New bridge module owning all MiroFish interaction | 1–6 |
| `/simulate/{id}` with async jobs | 8 |
| `/simulation/{id}` view | 9 |
| `sim_path` column | 7 |
| Seed construction + LLM expansion + quality gate | 2, 3 |
| Progress mapping 0/30/55/85/100 | 5 |
| Cost guard: `--max-rounds 12`, reuse unless force | 8 |
| Error handling: server down, Zep 401, partial report, timeout | 4, 5, 6 |
| Language must be explicit (`Accept-Language`) | 4 (`--lang en` hardcoded in `run_stage`) |
| Report completeness separate from `_report_complete` | 1 |
| Dead duplicate `deep_eval` removed | 7 |
| Tests as specified | 1–6, 8, 9 |
| UI button | 10 |

**Placeholder scan:** no TBD/TODO. Task 10 Steps 1 and 3 instruct reading existing markup before editing, because the button's interpolation syntax must match the surrounding template — the plan states exactly what to grep for and what shape to copy.

**Type consistency:** `run_simulation(title, meta, source, on_stage, out_path, evaluation)` is defined in Task 5 and called with exactly those keywords in Task 8. `read_report(path)` defined Task 6, used Tasks 8, 9, 11. `BridgeError` defined Task 4, raised Tasks 3–6, caught Task 8. `build_seed(title, meta, source, expansion, evaluation)` defined Task 2, called Task 5. `count_named_actors` and `check_seed_quality` defined Task 3, used Task 5. All Python 3.9-compatible (no PEP 604 unions, `Optional`/`Dict`/`Tuple` from `typing`, `%`-formatting in new code).
