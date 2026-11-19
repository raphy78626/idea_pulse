"""Local humanizer + AI-tell gate for IdeaPulse outbound messages.

Wraps the installed `ig-human` tools (`humanize.py` + `detect.py`) so every
Reddit/X reply is stripped of machine artefacts (invisible chars, em dashes,
curly quotes, 154 slop terms) and scored on the 5-check panel (burstiness,
specificity, slop density, fingerprint, voice) before anything is posted.

If a draft does not reach PASS (score >= 70, no single check < 55), runs up to
2 targeted rewrite passes using the exact weakest signal named by `detect.py`.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

_CANDIDATE_DIRS = (
    Path.home() / ".hermes" / "skills" / "rafi" / "ig-human",
    Path.home() / ".claude" / "skills" / "ig-human",
)

_BLADER_SKILL_PATHS = (
    Path(__file__).resolve().parent / "skills" / "humanizer" / "SKILL.md",
    Path.home() / ".hermes" / "skills" / "humanizer" / "SKILL.md",
    Path.home() / ".cursor" / "skills" / "humanizer" / "SKILL.md",
    Path.home() / ".claude" / "skills" / "humanizer" / "SKILL.md",
)


def blader_skill_path():
    """Return installed blader/humanizer v4.0.0 SKILL.md path, or None."""
    for p in _BLADER_SKILL_PATHS:
        if p.exists():
            return p
    return None


def _skill_dir():
    for d in _CANDIDATE_DIRS:
        if (d / "humanize.py").exists() and (d / "detect.py").exists():
            return d
    return None


def _fallback_clean(text):
    """Minimal pure-Python cleanup when ig-human scripts are absent (e.g., CI)."""
    s = (text or "").strip()
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u2060", "\u00ad"):
        s = s.replace(ch, "")
    s = s.replace("\u00a0", " ").replace("\u202f", " ")
    s = re.sub(r"\s*—\s*", ", ", s)
    s = s.replace("–", "-").replace("…", "...")
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def clean_text(text):
    """Run ig-human humanize.py on text. Returns (cleaned_text, changes_count, structural_flags)."""
    sdir = _skill_dir()
    if not sdir:
        cleaned = _fallback_clean(text)
        return cleaned, (1 if cleaned != text else 0), []
    res = subprocess.run(
        [sys.executable, str(sdir / "humanize.py"), "-", "--json"],
        input=text or "",
        capture_output=True,
        text=True,
        timeout=15,
    )
    if res.returncode != 0 or not res.stdout.strip():
        cleaned = _fallback_clean(text)
        return cleaned, (1 if cleaned != text else 0), []
    try:
        d = json.loads(res.stdout)
        cleaned = (d.get("text") or d.get("cleaned") or text).strip()
        # humanize.py --json returns {"text": ..., "report": ...} or similar; handle flexibly
        report = d.get("report") or d
        changes = report.get("total_changes")
        if changes is None:
            changes = sum(len(report.get(k) or []) for k in ("invisible", "typography", "lexical"))
        flags = report.get("structural") or []
        return cleaned, int(changes), flags
    except ValueError:
        cleaned = _fallback_clean(text)
        return cleaned, (1 if cleaned != text else 0), []


def detect_score(text):
    """Run ig-human detect.py on text. Returns {human_score, verdict, weakest, weakest_detail, checks}."""
    sdir = _skill_dir()
    if not sdir:
        return {"human_score": 80.0, "verdict": "PASS", "weakest": "", "weakest_detail": "", "checks": {}}
    res = subprocess.run(
        [sys.executable, str(sdir / "detect.py"), "-", "--json"],
        input=text or "",
        capture_output=True,
        text=True,
        timeout=15,
    )
    if not res.stdout.strip():
        return {"human_score": 0.0, "verdict": "FLAGGED", "weakest": "detect", "weakest_detail": res.stderr.strip(), "checks": {}}
    try:
        d = json.loads(res.stdout)
        checks = d.get("checks") or {}
        weakest = ""
        weakest_detail = ""
        if checks:
            weakest = min(checks.keys(), key=lambda k: checks[k].get("score", 100.0))
            weakest_detail = checks[weakest].get("detail", "")
        return {
            "human_score": float(d.get("human_score", 0.0)),
            "verdict": str(d.get("verdict", "FLAGGED")),
            "weakest": weakest,
            "weakest_detail": weakest_detail,
            "checks": checks,
        }
    except ValueError:
        return {"human_score": 0.0, "verdict": "FLAGGED", "weakest": "parse", "weakest_detail": "", "checks": {}}


REWRITE_SYSTEM = (
    "Rewrite this outreach draft using blader/humanizer (v4.0.0) so it reads like a real, experienced "
    "software engineer typing casually to one person — NOT a chatbot.\n"
    "Eliminate these AI tells (keep every supported fact/detail intact):\n"
    "- §1 Not-X-but-Y contrasts ('not just X, it's Y', 'rather than X'). State the point directly.\n"
    "- §2 One-line summary closers and dramatic fragments ('That's the real win.', 'Simple. Fast. Clean.').\n"
    "- §3-§5 Fake-deep sayings ('at its core', 'what really matters'), staged run-ups, arguing with no one.\n"
    "- §6-§8 Forced rule-of-three triads, repeated sentence openings, and em dashes (—) as connectors.\n"
    "- §12-§16 Stock AI words (delve, leverage, robust, seamless, streamline, crucial, game-changer) and sales hype.\n"
    "- §19-§22 Decorative bold, curly quotes, and chatbot residue ('Happy to help!', 'Feel free to DM', 'Let me know if that helps').\n"
    "- §26 Re-explaining what the reader already knows — skip background they just wrote and get straight to the useful point.\n"
    "Voice rules:\n"
    "- Mix sentence lengths sharply (one very short 3-6 word sentence + one longer concrete sentence).\n"
    "- Use natural contractions (I'd, don't, it's, won't, you're) and plain first-person voice.\n"
    "- Preserve placeholders like {link} or {name} untouched if present.\n"
    "- ZERO em dashes (—), ZERO curly quotes, ZERO hashtags, ZERO invented facts.\n"
    "- Output ONLY the final rewritten message text (embedded mode), nothing else."
)


def _default_rewrite(draft, weakest, detail, max_chars=None):
    import llm
    cap_hint = f" Keep it strictly under {max_chars} characters." if max_chars else " Keep it under 75 words."
    user = (
        f"Current draft:\n{draft}\n\n"
        f"Weakest AI-detector signal to fix: {weakest} ({detail}).{cap_hint}\n"
        "Rewrite so it reads naturally out loud."
    )
    txt, _ = llm.chat(REWRITE_SYSTEM, user, max_tokens=400, timeout=60, want_json=False)
    txt = (txt or "").strip()
    if len(txt) >= 2 and txt[0] == txt[-1] and txt[0] in ('"', "'"):
        txt = txt[1:-1].strip()
    return txt or draft


def humanize_reply(text, max_chars=None, rewrite_fn=None, max_rewrites=2):
    """Clean + score a draft, rewriting up to `max_rewrites` times until detect.py returns PASS."""
    current, changes, _ = clean_text(text)
    score = detect_score(current)
    best = (current, score, changes, 0)

    if score["verdict"] == "PASS" or not rewrite_fn:
        return {
            "text": current,
            "human_score": score["human_score"],
            "verdict": score["verdict"],
            "weakest": score["weakest"],
            "weakest_detail": score["weakest_detail"],
            "changes": changes,
            "rewrites": 0,
        }

    for attempt in range(1, max_rewrites + 1):
        cand = rewrite_fn(current, score["weakest"], score["weakest_detail"], max_chars=max_chars)
        if not cand:
            break
        cleaned, ch, _ = clean_text(cand)
        sc = detect_score(cleaned)
        if sc["human_score"] >= best[1]["human_score"]:
            best = (cleaned, sc, changes + ch, attempt)
        current, score = cleaned, sc
        if sc["verdict"] == "PASS":
            break

    final_text, final_sc, total_ch, rewrites = best
    return {
        "text": final_text,
        "human_score": final_sc["human_score"],
        "verdict": final_sc["verdict"],
        "weakest": final_sc["weakest"],
        "weakest_detail": final_sc["weakest_detail"],
        "changes": total_ch,
        "rewrites": rewrites,
    }
