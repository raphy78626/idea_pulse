"""Business / side-hustle idea sources — the "first currency" lane.

Launch directories (launch_scrape.py) show products other people already shipped.
That answers "should I build this?" but never "what could I sell next week?".
These sources answer the second question:

  - Reddit business/hustle subs, via the Arctic Shift archive. Direct reddit.com
    JSON has returned 403 from this machine since Oct 13, so the archive is the
    only way in (verified working 2026-11-08, ~1s/sub).
  - HN comments matching demand phrases ("I would pay for", "wish there was").
    A comment is not a business, but it is a named person stating a willingness
    to pay, which is the strongest free signal available.

Arctic Shift supports subreddit/limit/sort/after only — no score filter and no
full-text search — so intent and quality filtering happens locally.
"""
import html
import json
import re
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "IdeaPulse/1.0 (personal idea research)"}
ARCHIVE = "https://arctic-shift.photon-reddit.com/api/posts/search"
HN = "https://hn.algolia.com/api/v1/search"

# Where people actually describe starting something for money.
SUBREDDITS = [
    "sidehustle", "juststart", "Entrepreneur", "smallbusiness", "freelance",
    "consulting", "beermoney", "passive_income", "digitalnomad", "SaaS",
    "indiehackers", "webdev", "startups", "EntrepreneurRideAlong",
]

# A post is about earning money, not just chat. Any hit qualifies.
INTENT = (
    "how do i make", "how can i make", "first $", "first 100", "made my first",
    "what business", "side hustle", "side income", "want to start", "worth it",
    "i can code", "need money", "earn", "customer", "clients", "charge",
    "price", "paid me", "got paid", "freelanc", "offer", "niche", "monetize",
    "made $", "making $", "revenue", "invoice", "retainer",
)

# Posts that promise money but teach nothing.
NOISE = ("referral link", "sign up using", "use my code", "promo code",
         "giveaway", "hiring", "[hiring]", "for hire", "dm me", "telegram")

REMOVED = ("[removed]", "[deleted]", "")


def _clean(s, limit=400):
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _http_json(url, timeout=35, tries=2):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except Exception as e:          # 422 "slow down", timeouts, 5xx
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def _score(post):
    """Local heat score — Arctic Shift can't sort or filter by score."""
    return int(post.get("score") or 0) + 2 * int(post.get("num_comments") or 0)


def _wanted(post, min_score=5):
    body = post.get("selftext") or ""
    title = post.get("title") or ""
    if body.strip() in REMOVED or not title:
        return False
    if int(post.get("score") or 0) < min_score:
        return False
    hay = (title + " " + body).lower()
    if any(n in hay for n in NOISE):
        return False
    return any(k in hay for k in INTENT)


def reddit_hustles(client=None, per_sub=100, min_score=5, limit=40):
    """Recent business posts that pass the intent + quality filter."""
    rows, seen = [], set()
    for sub in SUBREDDITS:
        url = f"{ARCHIVE}?subreddit={sub}&limit={per_sub}&sort=desc"
        try:
            data = _http_json(url)
        except Exception:
            time.sleep(1.0)
            continue
        for p in data.get("data") or []:
            if not _wanted(p, min_score):
                continue
            url_ = "https://www.reddit.com" + (p.get("permalink") or "")
            if url_ in seen:
                continue
            seen.add(url_)
            title = _clean(p.get("title"), 150)
            body = _clean(p.get("selftext"), 300)
            meta = (f"r/{p.get('subreddit')} · {p.get('score', 0)}↑ · "
                    f"{p.get('num_comments', 0)} comments · {body}")
            rows.append((title, url_, meta[:340], "business"))
        time.sleep(1.1)             # the archive 422s if hit too fast
    rows.sort(key=lambda r: -len(r[2]))     # richer bodies first
    return rows[:limit]


def hn_demand(client=None, limit=25):
    """HN comments where someone states they would pay for a thing."""
    phrases = ['"I would pay for"', '"I\'d pay for"', '"wish there was a"',
               '"we would pay"', '"shut up and take my money"']
    out, seen = [], set()
    for phrase in phrases:
        q = urllib.parse.quote(phrase)
        url = f"{HN}?query={q}&tags=comment&hitsPerPage=15"
        try:
            data = _http_json(url, timeout=25)
        except Exception:
            continue
        for h in data.get("hits") or []:
            oid, text = h.get("objectID"), _clean(h.get("comment_text"), 260)
            if not oid or oid in seen or len(text) < 60:
                continue
            seen.add(oid)
            title = text[:110]
            thread = h.get("story_title") or "HN discussion"
            url_ = f"https://news.ycombinator.com/item?id={oid}"
            meta = f"HN comment on “{_clean(thread, 70)}” · {text}"
            out.append((title, url_, meta[:340], "demand"))
        time.sleep(0.4)
    return out[:limit]


ADAPTERS = {
    "Reddit business ideas": reddit_hustles,
    "HN demand signals": hn_demand,
}


def fetch(client=None):
    out = []
    for name, fn in ADAPTERS.items():
        try:
            out.extend(fn(client))
        except Exception:
            pass
    return out


if __name__ == "__main__":
    import sys
    items = fetch()
    print(f"{len(items)} items\n")
    for t, u, m, k in items[:14]:
        print(f"[{k:<8}] {t[:72]}")
        print(f"           {m[:110]}")
