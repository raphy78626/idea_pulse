"""Bridge from IdeaPulse to the MiroFish reaction simulator.

Talks to the foresight skill CLI rather than MiroFish's HTTP API directly, so the
pipeline's hard-won fixes (one shared graph handle per process, explicit
close-env, Kuzu RelatesToNode_ edges, ontology-derived entity types) live in one
place instead of being duplicated here.

Python 3.9: no PEP 604 unions, no builtin generic aliases.
"""
import html
import json
import os
import re
import subprocess
import time
from pathlib import Path
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class BridgeError(RuntimeError):
    """A stage failed in a way the caller must surface, never swallow."""


MIN_REPORT_CHARS = 200
MIN_NAMED_ACTORS = 3
SKILL_SCRIPTS = Path.home() / ".hermes" / "skills" / "mlops" / "foresight" / "scripts"
STAGE_TIMEOUT_S = 1500
REPORT_RETRY_ATTEMPTS = 8
REPORT_RETRY_WAIT_S = 5
MAX_TITLE_CHARS = 400
MAX_META_CHARS = 1200

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

# Sentence terminators across the languages MiroFish can emit reports in.
_SENTENCE_END = re.compile(r"[.!?\"')\]\u3002\uff01\uff1f\u201d\u300d\u300f]\s*$")

# Models occasionally write their deliberation into the report: XML tool-call
# blocks and "Thought:" notes. MiroFish has no sanitizer for this, and the
# leakage is model-dependent, so the bridge cleans the artifact it owns.
_XML_BLOCK = re.compile(r"<(tool_call|thinking)\b[^>]*>.*?</\1>", re.S | re.I)
_XML_OPEN_TAIL = re.compile(r"<(tool_call|thinking)\b[^>]*>.*\Z", re.S | re.I)
_THOUGHT_LINE = re.compile(r"^\s*thought\s*:\s*", re.I)


def sanitize_report(text):
    # type: (Optional[str]) -> str
    """Strip model deliberation that leaked into the report body."""
    if not text:
        return ""
    needs_work = (
        _XML_BLOCK.search(text) is not None
        or _XML_OPEN_TAIL.search(text) is not None
        or any(_THOUGHT_LINE.match(line) for line in text.split("\n"))
    )
    if not needs_work:
        return text
    out = _XML_BLOCK.sub("", text)
    out = _XML_OPEN_TAIL.sub("", out)
    kept = []
    dropping = False
    for line in out.split("\n"):
        if _THOUGHT_LINE.match(line):
            dropping = True
            continue
        if dropping:
            if line.strip():
                continue
            dropping = False
            continue
        kept.append(line)
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip() + "\n" if out.strip() else ""


def report_is_complete(markdown):
    # type: (str) -> bool
    """Language-independent completeness: substantial text ending a sentence.

    Deliberately separate from app._report_complete, which requires a
    '## Final call' heading that only deep-eval reports have.
    """
    if not markdown or len(markdown.strip()) < MIN_REPORT_CHARS:
        return False
    return bool(_SENTENCE_END.search(markdown.rstrip()))


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

# A participant line is `Label - description` (or en/em dash), the label being
# 1-60 chars with no colon. Anchored so prose containing a hyphen cannot match.
_ACTOR_LINE = re.compile(r"^[ \t]*([^:\n]{1,60}?)\s*[\u2013\u2014-]\s+\S", re.M)


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


def _env_file_candidates():
    """Where MiroFish keeps its .env, in the order its own launcher looks."""
    return [
        Path.home() / ".hermes/tools/prediction/mirofish/.env",
        Path.home() / ".hermes/.env",
    ]


def _local_zep_path():
    """The file install_local_graph.py patches. A seam so tests can substitute."""
    return (Path.home() / ".hermes/tools/prediction/mirofish"
            / "backend/app/utils/zep.py")


def graph_backend_problem():
    """Detect a graph backend that genuinely cannot build.

    Correction to an earlier, wrong version of this function: the placeholder
    ZEP_API_KEY is NOT a problem. MiroFish is installed with the Hermes local
    graph backend (install_local_graph.py), which overrides get_zep_client() to
    use Graphiti+Kuzu and authenticates with LLM_API_KEY, not Zep. Upstream's
    `if not Config.ZEP_API_KEY` guards only test for a non-empty string, so the
    placeholder exists to satisfy them while the local client does the work.
    Simulations have completed under exactly this config (6, 1 and 6 entities),
    so requiring a real Zep key would break a working setup.

    What actually matters is that the local override is present and its LLM
    credentials exist.
    """
    # 1. Is the local backend installed? Without it, Zep Cloud is required.
    zep_py = _local_zep_path()
    try:
        installed = "hermes local graph backend" in zep_py.read_text()
    except OSError:
        installed = False

    if not installed:
        key = ""
        for path in _env_file_candidates():
            try:
                for line in path.read_text().splitlines():
                    if line.strip().startswith("ZEP_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            except OSError:
                continue
            if key:
                break
        if not key or "placeholder" in key.lower():
            return ("the Hermes local graph backend is not installed and no real "
                    "ZEP_API_KEY is set. Install the local backend (no account "
                    "needed): ~/.hermes/tools/prediction/.venv/bin/python "
                    "~/.hermes/skills/mlops/foresight/scripts/install_local_graph.py "
                    "— or get a free Zep key at https://app.getzep.com/")
        return ""

    # 2. Local backend present: it needs LLM credentials, not Zep.
    llm_key = ""
    for path in _env_file_candidates():
        try:
            for line in path.read_text().splitlines():
                if line.strip().startswith("LLM_API_KEY="):
                    llm_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            continue
        if llm_key:
            break
    if not llm_key:
        return ("the local graph backend needs LLM_API_KEY in MiroFish's .env "
                "(it builds the graph with your own model calls, not Zep). "
                "Add LLM_API_KEY=sk-or-... and restart: "
                "~/.hermes/skills/mlops/foresight/scripts/mirofish_server.sh restart")
    return ""


def preflight(check_graph=True):
    # type: (bool) -> Tuple[bool, str]
    """Confirm the MiroFish server is reachable; return a remedy hint on failure.

    check_graph also validates the graph backend credentials, because a healthy
    server with a placeholder key still cannot build a graph — it just fails
    slowly. Pass check_graph=False only to probe process liveness.
    """
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

    if check_graph:
        problem = graph_backend_problem()
        if problem:
            return False, problem
    return True, ""


def _sleep(seconds):
    time.sleep(seconds)


# MiroFish's simulate stage flushes its terminal state asynchronously, so the
# report API can briefly return 409 "still active" right after a finished run.
_SIM_STILL_ACTIVE = "still active"


def is_transient_report_rejection(message):
    # type: (str) -> bool
    """True only for the known racy 'simulation still active' 409 case."""
    return bool(message) and _SIM_STILL_ACTIVE in message.lower()


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
            reason
            + " The idea record is too thin to simulate; add context or name the players."
            + " (expansion length: %d characters, expansion model: %s)"
            % (len(expansion), model)
        )
    say_once(2, "market mapped with %d participants" % count_named_actors(expansion))

    seed = build_seed(title, meta, source, expansion=expansion, evaluation=evaluation)
    workdir = Path(os.environ.get("TMPDIR") or "/tmp") / "ideapulse-sim"
    workdir.mkdir(parents=True, exist_ok=True)
    seed_path = workdir / ("seed-%s.txt" % abs(hash((title or "") + (meta or ""))))
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
            attempt = 0
            while True:
                try:
                    data = run_stage(report_args)
                    break
                except BridgeError as exc:
                    attempt += 1
                    if (attempt >= REPORT_RETRY_ATTEMPTS
                            or not is_transient_report_rejection(str(exc))):
                        raise
                    say_once(hi, "waiting for the simulation to settle (%d/%d)"
                             % (attempt, REPORT_RETRY_ATTEMPTS))
                    _sleep(REPORT_RETRY_WAIT_S * attempt)
            saved = str(data.get("saved_to") or saved)
        say_once(hi, label)

    return {
        "project_id": project_id,
        "simulation_id": simulation_id,
        "report_path": saved,
        "expansion_model": model,
    }


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
    if not text:
        return ""
    return sanitize_report(text) if report_is_complete(text) else ""
