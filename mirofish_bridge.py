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

MIN_REPORT_CHARS = 200
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
