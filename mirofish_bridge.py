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
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class BridgeError(RuntimeError):
    """A stage failed in a way the caller must surface, never swallow."""


MIN_REPORT_CHARS = 200
MIN_NAMED_ACTORS = 3
SKILL_SCRIPTS = Path.home() / ".hermes" / "skills" / "mlops" / "foresight" / "scripts"
STAGE_TIMEOUT_S = 1500
MAX_TITLE_CHARS = 400
MAX_META_CHARS = 1200


def report_is_complete(markdown):
    # type: (str) -> bool
    """A MiroFish report is complete when it is substantial and ends a sentence.

    Deliberately separate from app._report_complete, which requires a
    '## Final call' heading that only deep-eval reports have.
    """
    if not markdown or len(markdown.strip()) < MIN_REPORT_CHARS:
        return False
    return bool(re.search(r"[.!?\"')\]]\s*$", markdown.rstrip()))


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

# Capitalised multiword or dotted names on a dash-bulleted line, e.g. "Apollo.io - ..."
_ACTOR_LINE = re.compile(r"^\s*([A-Z][\w.'&-]*(?:\s+[A-Z][\w.'&-]*)*)\s*[-\u2013\u2014]", re.M)


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
