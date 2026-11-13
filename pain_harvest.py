"""Harvest Reddit posts where people describe a pain, via the Arctic Shift archive.

Why this exists (PLAN_FIRST_DOLLAR.md, stage 1): reddit.com/*.json has answered
403 from this machine since mid-October, and the other feeds are launch
announcements, not problems. Buyers describe their problems in a handful of
subreddits; this pulls the newest posts there and keeps the ones that say so.

How it behaves:
  - one newest-first listing of up to 100 posts per subreddit, with only the
    fields we use (the archive rejects full-text search on large subreddits);
  - the pain filter runs locally on title + body;
  - requests are paced (Arctic Shift answers "Maybe slow down a bit" with a
    422 when hit quickly) and a slow-down answer is retried once after a
    longer wait;
  - a subreddit that fails is skipped, but if every one fails the whole
    source raises, so the dashboard shows FAILED instead of a silent "ok".

Items come back as (title, url, meta, "pain", body, author) for save_items().
"""
import re
import time

API = "https://arctic-shift.photon-reddit.com/api/posts/search"
FIELDS = "id,title,selftext,author,created_utc,score,num_comments,subreddit"

# Where buyers describe problems (plan §2). Kept to ~12 so a nightly run with
# pacing stays around a minute.
PAIN_SUBS = ["smallbusiness", "Entrepreneur", "freelance", "ecommerce", "SaaS",
             "sysadmin", "Bookkeeping", "FulfillmentByAmazon", "shopify",
             "msp", "realtors", "agency"]

PAUSE = 4.0      # seconds between requests
BACKOFF = 15.0   # wait before the single retry after a "slow down" answer
LIMIT = 100

# Phrases people use when they have a problem worth paying to solve (plan §2).
PAIN_PATTERNS = [
    r"is there (?:a|an|any) (?:tool|app|software|service|way|plugin|extension)",
    r"any (?:tool|app|software|service)s? (?:for|that|to)",
    r"i wish (?:there was|there were|someone|i could)",
    r"(?:i'?d|i would|we'?d|we would)(?: happily| gladly)? pay",
    r"spend(?:s|ing)? (?:hours|so much time|way too much time)",
    r"wast(?:e|es|ed|ing) (?:hours|so much time)",
    r"\bmanually\b",
    r"\bby hand\b",
    r"hate (?:doing|having to)",
    r"tired of",
    r"alternative to",
    r"so frustrating",
    r"how do (?:you|y'all|you all|others) (?:handle|manage|track|deal with|keep track)",
]
_PAIN_RE = re.compile("|".join("(?:%s)" % p for p in PAIN_PATTERNS), re.I)

_EMPTY_BODIES = {"", "[removed]", "[deleted]"}


def pain_signal(text):
    """The first pain phrase found in text, lower-cased, or '' if none."""
    m = _PAIN_RE.search(text or "")
    return m.group(0).lower() if m else ""


def _slow_down(resp):
    if resp.status_code == 429:
        return True
    if resp.status_code == 422:
        try:
            return "slow down" in (resp.json().get("error") or "").lower()
        except Exception:
            return False
    return False


def _listing(client, sub, sleep):
    params = {"subreddit": sub, "limit": LIMIT, "sort": "desc", "fields": FIELDS}
    resp = client.get(API, params=params, timeout=30)
    if _slow_down(resp):
        sleep(BACKOFF)
        resp = client.get(API, params=params, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError("HTTP %s for r/%s" % (resp.status_code, sub))
    return resp.json().get("data") or []


def _to_item(p):
    title = (p.get("title") or "").strip()
    body = (p.get("selftext") or "").strip()
    if body in _EMPTY_BODIES:
        body = ""
    hit = pain_signal(title + "\n" + body)
    if not title or not hit:
        return None
    sub = p.get("subreddit") or ""
    url = "https://www.reddit.com/r/%s/comments/%s/" % (sub, p.get("id"))
    meta = "r/%s · %s ↑ · %s comments · “%s”" % (
        sub, p.get("score", 0), p.get("num_comments", 0), hit)
    return (title[:200], url, meta[:120], "pain", body, p.get("author") or "")


def fetch(client, subs=None, sleep=time.sleep, pause=PAUSE):
    """Newest pain posts across subs. Raises only if every subreddit failed."""
    subs = PAIN_SUBS if subs is None else subs
    items, errors, seen = [], [], set()
    for i, sub in enumerate(subs):
        if i:
            sleep(pause)
        try:
            posts = _listing(client, sub, sleep)
        except Exception as e:  # one bad subreddit must not lose the others
            errors.append(str(e))
            continue
        for p in posts:
            item = _to_item(p)
            # the same author cross-posting to several subreddits is one pain,
            # not several — counting it twice would inflate demand later
            key = (item[5].lower(), item[0].lower()) if item else None
            if item and key not in seen:
                seen.add(key)
                items.append(item)
    if subs and len(errors) == len(subs):
        raise RuntimeError("all subreddits failed: " + errors[0])
    return items
