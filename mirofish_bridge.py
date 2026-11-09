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
