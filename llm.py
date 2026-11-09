"""Free-model chat for IdeaPulse: discovers OpenRouter's current free models daily,
health-checks them, cools down rate-limited ones, and falls back to local Ollama."""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

BASE = Path(__file__).parent
CACHE = BASE / ".free_models.json"
CACHE_TTL_S = 24 * 3600
OR_BASE = "https://openrouter.ai/api/v1"
# openrouter/free is a random router; inkling only serves agent harnesses; the rest aren't chat models
EXCLUDE = ("openrouter/free", "inkling", "lyria", "safety", "guard", "embed")
# soft ordering only: names missing from today's free list are ignored, unknown models go after these
PREFER = ("nemotron-3.5-super", "dots-3", "gemma-4-31b", "nemotron-3-ultra", "nemotron-3.5-lightning")
LOCAL_MODEL = os.environ.get("IDEAPULSE_LOCAL_MODEL", "qwen3:8b")
OLLAMA = "http://localhost:11434/api/chat"

_cooldown = {}


def _load_key():
    if os.environ.get("OPENROUTER_API_KEY"):
        return
    for rc in (Path.home() / ".config/openrouter/env", Path.home() / ".zshrc"):
        try:
            for line in open(rc):
                line = line.strip()
                if line.startswith("export ") and "OPENROUTER_API_KEY=" in line:
                    k, _, v = line[7:].partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            pass


_load_key()


def _key():
    return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY", "")


def parse_json(text):
    """Model JSON is often fenced, padded with prose, or has raw newlines in strings."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    raw = re.sub(r",\s*([}\]])", r"\1", m.group(0))
    try:
        return json.loads(raw, strict=False)
    except ValueError:
        return None


def _post(model, system, user, max_tokens, timeout):
    """Returns (text, error, retry_after_s)."""
    try:
        r = httpx.post(f"{OR_BASE}/chat/completions", timeout=timeout,
                       headers={"Authorization": f"Bearer {_key()}"},
                       json={"model": model, "max_tokens": max_tokens,
                             "reasoning": {"exclude": True},
                             "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}]})
    except httpx.HTTPError as e:
        return "", f"{type(e).__name__}", 0
    try:
        body = json.loads(r.text, strict=False)
    except ValueError:
        return "", f"HTTP {r.status_code} non-json", 0
    err = body.get("error") if isinstance(body, dict) else None
    if r.status_code == 429 or (err and err.get("code") == 429):
        try:
            wait = int(r.headers.get("retry-after") or 60)
        except ValueError:
            wait = 60
        return "", "429 rate limit", wait
    if r.status_code != 200 or err or not body.get("choices"):
        code = (err or {}).get("code", r.status_code)
        return "", f"HTTP {code}", (24 * 3600 if code in (403, 404) else 0)
    text = (body["choices"][0].get("message", {}).get("content") or "").strip()
    return (text, "", 0) if text else ("", "empty content", 0)


def _rank(model):
    return next((i for i, pat in enumerate(PREFER) if pat in model), len(PREFER))


def _probe(model):
    t0 = time.time()
    text, err, _ = _post(model, "Reply with JSON only.",
                         'Return {"ok": true, "n": 3}', max_tokens=600, timeout=45)
    ok = bool(parse_json(text))
    return {"id": model, "ok": ok, "latency": round(time.time() - t0, 1), "error": err}


def discover(force=False):
    """Current free chat models, healthy ones first by latency. Cached for a day."""
    if not force and CACHE.exists() and time.time() - CACHE.stat().st_mtime < CACHE_TTL_S:
        try:
            models = json.loads(CACHE.read_text())["models"]
            if models:
                return models
        except (ValueError, KeyError):
            pass
    try:
        data = httpx.get(f"{OR_BASE}/models", timeout=30).json()["data"]
    except (httpx.HTTPError, ValueError, KeyError):
        return json.loads(CACHE.read_text())["models"] if CACHE.exists() else []
    ids = []
    for m in data:
        p = m.get("pricing") or {}
        free = m["id"].endswith(":free") or (p.get("prompt") == "0" and p.get("completion") == "0")
        text_out = "text" in ((m.get("architecture") or {}).get("output_modalities") or ["text"])
        if free and text_out and not any(x in m["id"] for x in EXCLUDE):
            ids.append(m["id"])
    with ThreadPoolExecutor(max_workers=6) as ex:
        probes = list(ex.map(_probe, ids))
    # unhealthy ones are kept at the back: a 429 during the probe is usually transient
    probes = [p for p in probes if p["error"] not in ("HTTP 403", "HTTP 404")]
    probes.sort(key=lambda p: (not p["ok"], _rank(p["id"]), p["latency"]))
    CACHE.write_text(json.dumps({"checked": time.strftime("%Y-%m-%d %H:%M"), "models": probes}, indent=1))
    return probes


def _local(system, user, max_tokens, timeout):
    sys.path.insert(0, str(Path.home() / "bin"))
    try:
        from _ollama_lock import ollama_lock
    except ImportError:
        ollama_lock = None
    payload = {"model": LOCAL_MODEL, "stream": False, "think": False,
               "options": {"num_predict": max_tokens},
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    try:
        if ollama_lock:
            with ollama_lock():
                r = httpx.post(OLLAMA, json=payload, timeout=timeout)
        else:
            r = httpx.post(OLLAMA, json=payload, timeout=timeout)
        return (r.json().get("message", {}).get("content") or "").strip()
    except (httpx.HTTPError, ValueError, TimeoutError):
        return ""


def chat(system, user, max_tokens=1200, timeout=90, want_json=False, local_fallback=True,
         max_models=6):
    """Returns (text, model). On total failure text is '' and model is the last error.

    Reasoning models spend the *output* budget on hidden thinking, so a prompt that
    needs ~300 visible tokens can still return empty content at max_tokens=1400.
    Rather than abandon that model and walk the whole chain (which made one item
    look frozen for minutes), we retry the same model once with a much larger
    budget — cheaper than trying 13 different models.
    """
    last = "no OPENROUTER_API_KEY" if not _key() else "no healthy free model"
    if _key():
        tried = 0
        for m in [p["id"] for p in discover()]:
            if tried >= max_models:
                break
            if _cooldown.get(m, 0) > time.time():
                continue
            tried += 1
            text, err, wait = _post(m, system, user, max_tokens, timeout)
            if wait:
                _cooldown[m] = time.time() + wait
            # empty content almost always means the reasoning ate the budget
            if not text and err == "empty content":
                bigger = max(4000, max_tokens * 3)
                text, err2, wait2 = _post(m, system, user, bigger, timeout)
                if wait2:
                    _cooldown[m] = time.time() + wait2
                err = err2 or err
            if text and (not want_json or parse_json(text)):
                return text, m
            last = f"{m}: {err or 'no json'}"
    if local_fallback:
        text = _local(system, user, max_tokens, timeout * 2)
        if text and (not want_json or parse_json(text)):
            return text, f"ollama/{LOCAL_MODEL}"
    return "", last


if __name__ == "__main__":
    for p in discover(force="--refresh" in sys.argv):
        print(f"{'ok ' if p['ok'] else 'BAD'} {p['latency']:>5}s  {p['id']}  {p['error']}")
