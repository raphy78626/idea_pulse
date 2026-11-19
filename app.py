import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as quote_url

import html

import httpx
import mirofish_bridge
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

BASE = Path(__file__).parent
DB = BASE / "ideas.db"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) IdeaPulse/1.0"}

HOT_WORDS = ("ai", "agent", "saas", "bot", "automation", "tool", "app", "mcp",
             "whatsapp", "chrome extension", "micro", "n8n", "workflow", "voice")

SUBREDDITS = ["SomebodyMakeThis", "AppIdeas", "Startup_Ideas", "Business_Ideas",
              "CrazyIdeas", "SaaS", "EntrepreneurRideAlong", "indiehackers"]


def db():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_on TEXT, source TEXT, title TEXT, url TEXT UNIQUE,
        meta TEXT, score INTEGER, kind TEXT, eng INTEGER DEFAULT 0)""")
    for col in ("eng INTEGER DEFAULT 0", "verdict TEXT", "buildability INTEGER",
                "ttm_days INTEGER", "what TEXT", "reason TEXT", "report_path TEXT", "nutshell TEXT",
                "pitch TEXT", "capital INTEGER", "first_payment TEXT", "earn_verdict TEXT",
                "proof TEXT", "buyer TEXT", "offer_money TEXT", "sim_path TEXT",
                "sim_attempts INTEGER DEFAULT 0", "sim_error TEXT",
                "body TEXT", "author TEXT"):
        try:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col}")
        except Exception:
            pass
    return conn


def engagement(meta):
    m = re.search(r"(\d[\d,]*)", meta or "")
    return int(m.group(1).replace(",", "")) if m else 0


def save_items(source, items):
    """Insert new items; returns how many were new.

    Each item is (title, url, meta, kind) or, for sources that have the full
    post, (title, url, meta, kind, body, author).
    """
    conn = db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    n = 0
    for item in items:
        title, url, meta, kind = item[:4]
        body, author = (tuple(item[4:6]) + (None, None))[:2]
        score = sum(2 for w in HOT_WORDS if w in title.lower())
        cur = conn.execute("INSERT OR IGNORE INTO items (fetched_on, source, title, url, meta, score, kind, eng,"
                           " body, author) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (now, source, title, url, meta, score, kind, engagement(meta), body, author))
        n += max(0, cur.rowcount)
    conn.commit()
    conn.close()
    return n


def fetch_hn(client):
    r = client.get("https://hn.algolia.com/api/v1/search_by_date?tags=show_hn&hitsPerPage=40&numericFilters=points%3E5")
    items = []
    for h in r.json().get("hits", []):
        title = h.get("title") or ""
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        items.append((title, url, f"{h.get('points', 0)} pts · {h.get('num_comments', 0)} comments", "show_hn"))
    return items[:25]


def fetch_github(client):
    r = client.get("https://github.com/trending?since=daily")
    html = r.text
    items = []
    blocks = re.findall(r'<h2 class="h3 lh-condensed">.*?href="/([^"]+)"', html, re.S)
    descs = re.findall(r'<p class="col-9[^"]*">\s*(.*?)\s*</p>', html, re.S)
    stars = re.findall(r'href="/([^/]+/[^/]+)/stargazers.*?\s*([\d,]+)\s*</a>', html, re.S)
    for i, repo in enumerate(blocks[:20]):
        repo = repo.strip()
        desc = re.sub(r"<[^>]+>", "", descs[i]).strip() if i < len(descs) else ""
        st = stars[i][1].replace(",", "") if i < len(stars) else "?"
        items.append((f"{repo}: {desc}"[:160], f"https://github.com/{repo}", f"★ {st} today", "repo"))
    return items


def fetch_reddit(client):
    # reddit.com has answered 403 from this machine since mid-October. Count the
    # failures so a fully blocked run raises (dashboard: FAILED) instead of
    # returning [] and being reported as "ok". Pain posts come from
    # pain_harvest.py via Arctic Shift instead.
    items = []
    ok_subs, last_err = 0, ""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
    for sub in SUBREDDITS:
        for host in ("https://www.reddit.com", "https://old.reddit.com"):
            try:
                r = client.get(f"{host}/r/{sub}/top.json?t=day&limit=10", headers=headers, timeout=10)
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    continue
                children = r.json().get("data", {}).get("children", [])
                for c in children:
                    d = c["data"]
                    items.append((d["title"], f"https://reddit.com{d['permalink']}",
                                  f"r/{sub} · {d['score']} ↑ · {d['num_comments']} comments", "post"))
                ok_subs += 1   # only after the body parsed as a real listing
                break
            except Exception as e:
                last_err = type(e).__name__
                continue
    if SUBREDDITS and not ok_subs:
        raise RuntimeError(f"every subreddit failed ({last_err})")
    return items


def fetch_ph(client):
    """Product Hunt serves Atom (<entry>), not RSS (<item>) — parsing <item> always gave 0."""
    try:
        r = client.get("https://www.producthunt.com/feed", timeout=15)
        out = []
        for block in re.findall(r"<entry>(.*?)</entry>", r.text, re.S)[:20]:
            t = re.search(r"<title[^>]*>(.*?)</title>", block, re.S)
            l = re.search(r'<link[^>]+href="([^"]+)"', block)
            if not t:
                continue
            name = re.sub(r"<[^>]+>", "", t.group(1)).strip()
            url = (l.group(1).strip() if l else "https://producthunt.com").split("?")[0]
            if not name or not url.startswith("http"):
                continue
            # the tagline is the first <p> inside the (escaped) content block
            tagline = ""
            c = re.search(r"<content[^>]*>(.*?)</content>", block, re.S)
            if c:
                fp = re.search(r"<p>(.*?)</p>", html.unescape(c.group(1)), re.S)
                if fp:
                    tagline = re.sub(r"<[^>]+>", " ", fp.group(1))
                    tagline = re.sub(r"\s+", " ", tagline).strip()[:180]
            out.append((name[:150], url, tagline or "Product Hunt launch", "launch"))
        return out
    except Exception:
        return []


def fetch_askhn(client):
    r = client.get("https://hn.algolia.com/api/v1/search_by_date?tags=ask_hn&hitsPerPage=30&numericFilters=points%3E8")
    items = []
    for h in r.json().get("hits", []):
        title = h.get("title") or ""
        if not any(k in title.lower() for k in ("idea", "build", "saas", "side project", "who ", "anyone", "worth")):
            continue
        url = f"https://news.ycombinator.com/item?id={h['objectID']}"
        items.append((title, url, f"{h.get('points', 0)} pts · {h.get('num_comments', 0)} comments", "ask_hn"))
    return items[:12]


def fetch_devto(client):
    r = client.get("https://dev.to/api/articles?top=1&per_page=15")
    items = []
    for a in r.json():
        tags = a.get("tag_list") or []
        tags = [t if isinstance(t, str) else t.get("name", "") for t in tags][:3]
        items.append((f"{a['title']} (#{', '.join(tags)})",
                      a["url"], f"dev.to · {a.get('positive_reactions_count', 0)} ♥", "article"))
    return items[:12]


def fetch_gtrends(client):
    r = client.get("https://trends.google.com/trending/rss?geo=IN", timeout=10)
    items = []
    for block in re.findall(r"<item>(.*?)</item>", r.text, re.S)[:15]:
        t = re.search(r"<title>(.*?)</title>", block, re.S)
        approx = re.search(r"<ht:approx_traffic>(.*?)</ht:approx_traffic>", block, re.S)
        n = re.search(r"<news_item_title>(.*?)</news_item_title>", block, re.S)
        if not t:
            continue
        title = f"INDIA searching: {t.group(1).strip()}"
        if n:
            title += f" — {n.group(1)[:80]}"
        url = f"https://trends.google.com/trends/explore?q={t.group(1).strip().replace(' ', '+')}"
        items.append((title[:180], url, f"🔍 {approx.group(1) if approx else '?'} searches", "trend"))
    return items


def _dig(d, path):
    if not path:
        return d
    cur = d
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def fetch_declarative(client, cfg):
    r = client.get(cfg["url"], timeout=15)
    if cfg.get("type") == "json":
        data = r.json()
        rows = _dig(data, cfg.get("items_path", "")) or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        f = cfg.get("fields", {})
        out = []
        for row in rows:
            title = str(_dig(row, f.get("title", "title")) or "")[:160]
            url = str(_dig(row, f.get("url", "url")) or "")
            if url.startswith("/"):
                from urllib.parse import urljoin
                url = urljoin(cfg["url"], url)
            meta_raw = str(_dig(row, f.get("meta", "score")) or "?")
            meta = f.get("meta_template", "{meta}").replace("{meta}", meta_raw)
            if title and url.startswith("http"):
                out.append((title, url, meta[:90], "custom"))
        return out
    html = r.text
    out = []
    for m in re.finditer(cfg.get("item_regex", ""), html, re.S):
        g = m.groups()
        out.append((re.sub(r"<[^>]+>", "", g[0])[:160], g[1], (g[2] if len(g) > 2 else "")[:90], "custom"))
    return out


def fetch_apify_actor(client, cfg_actor, token):
    r = client.get(f"https://api.apify.com/v2/acts/{cfg_actor['actor']}/runs/last/dataset/items",
                   params={"token": token, "limit": cfg_actor.get("max_items", 20), "clean": "true"}, timeout=30)
    if r.status_code == 404:
        return [], "no runs yet (run the actor once in Apify console)"
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    f = cfg_actor.get("fields", {})
    out = []
    for row in r.json():
        title = str(_dig(row, f.get("title", "title")) or "")[:160]
        url = str(_dig(row, f.get("url", "url")) or "")
        url = url.format(**{k: str(v) for k, v in row.items()}) if "{" in url else url
        meta_raw = str(_dig(row, f.get("meta", "score")) or "?")
        meta = f.get("meta_template", "{meta}").replace("{meta}", meta_raw)
        if title and url:
            out.append((title, url, meta[:90], "apify"))
    return out, "ok"


def discover_sources():
    """built-ins + sources/*.py + sources/*.json + launch-directory scrapers"""
    srcs = [("HN Show HN", fetch_hn), ("Ask HN ideas", fetch_askhn), ("GitHub Trending", fetch_github),
            ("Reddit", fetch_reddit), ("Product Hunt", fetch_ph), ("dev.to", fetch_devto),
            ("Google Trends IN", fetch_gtrends)]
    # pain posts (people describing a problem) — the source the plan is built on
    try:
        import pain_harvest
        srcs.append(("Reddit pains", pain_harvest.fetch))
    except Exception:
        pass
    # launch directories that expose real product data (see launch_scrape.py)
    try:
        import launch_scrape
        for name, fn in launch_scrape.ADAPTERS.items():
            srcs.append((name, fn))
    except Exception:
        pass
    # business / side-hustle lanes — the "first currency" question (business_scrape.py)
    try:
        import business_scrape
        for name, fn in business_scrape.ADAPTERS.items():
            srcs.append((name, fn))
    except Exception:
        pass
    sdir = BASE / "sources"
    if sdir.exists():
        import importlib.util
        for pyf in sorted(sdir.glob("*.py")):
            try:
                spec = importlib.util.spec_from_file_location(pyf.stem, pyf)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                srcs.append((f"{pyf.stem} (custom)", mod.fetch))
            except Exception:
                pass
        for jsf in sorted(sdir.glob("*.json")):
            try:
                cfg = json.loads(jsf.read_text())
                srcs.append((cfg.get("name", jsf.stem), (lambda c: (lambda client: fetch_declarative(client, c)))(cfg)))
            except Exception:
                pass
    return srcs


def _status():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS source_status (
        source TEXT PRIMARY KEY, last_run TEXT, status TEXT, count INTEGER)""")
    conn.commit(); conn.close()

def set_status(source, status, count):
    conn = db(); _status()
    conn.execute("INSERT OR REPLACE INTO source_status VALUES (?,?,?,?)",
                 (source, datetime.now().strftime("%H:%M"), status, count))
    conn.commit(); conn.close()

def get_statuses():
    try:
        conn = db(); conn.row_factory = sqlite3.Row; _status()
        rows = [dict(r) for r in conn.execute("SELECT * FROM source_status ORDER BY source").fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def fetch_all():
    total = 0
    report = []
    with httpx.Client(headers=UA, timeout=15, follow_redirects=True) as client:
        for name, fn in discover_sources():
            try:
                fetched = fn(client)
                n = save_items(name, fetched)
                # n counts only NEW rows, so 0 can be healthy (nothing new today).
                # A source that returned nothing at all is broken or blocked, and
                # must not be reported as "ok" (Reddit sat at "ok +0" for a month).
                set_status(name, "ok" if fetched else "EMPTY: returned nothing", n)
                report.append(f"{name}: +{n}")
                total += n
            except Exception as e:
                set_status(name, f"FAILED {type(e).__name__}", 0)
                report.append(f"{name}: FAILED ({type(e).__name__})")
        # apify actors (token optional)
        try:
            cfg = json.loads((BASE / "sources_config.json").read_text())
            apify = cfg.get("apify", {})
            token = os.environ.get(apify.get("token_env", "APIFY_TOKEN"), "")
            for actor_cfg in apify.get("actors", []):
                name = actor_cfg.get("name", actor_cfg["actor"])
                if not token:
                    set_status(name, "skipped: set APIFY_TOKEN", 0)
                    continue
                try:
                    items, st = fetch_apify_actor(client, actor_cfg, token)
                    n = save_items(name, items)
                    set_status(name, f"ok ({st})", n)
                    report.append(f"{name}: +{n}")
                    total += n
                except Exception as e:
                    set_status(name, f"FAILED {type(e).__name__}", 0)
                    report.append(f"{name}: FAILED")
        except Exception:
            pass
    return total, report


EVAL_SYSTEM = ("You are a ruthless startup evaluator for a solo developer in India building "
               "micro-products for fast cash (first rupee within weeks). Judge ONLY what is "
               "stated. Reply with ONLY compact JSON, no markdown fences, keys: "
               '{"verdict":"BUILD|WATCH|SKIP","buildability":1-10,"ttm_days":number,'
               '"what":"one line: exactly what to build","reason":"one line why"}. '
               "BUILD = solo-buildable in <=1 week, clear buyer, fast first rupee. "
               "WATCH = interesting but distribution or demand unclear. "
               "SKIP = toy, needs scale/network effects, regulated, or hardware-heavy.")


# The "first currency" rubric. Different question from EVAL_SYSTEM:
# that one asks "is this a good product?"; this one asks "can this specific
# person get paid soon, and what is the shortest honest path to it?"
MONEY_SYSTEM = (
    "You evaluate opportunities for ONE specific person: a software engineer in India "
    "(Java/Spring, FastAPI, Python, WhatsApp bots, AI/LLM integration, some frontend) "
    "who wants to earn their FIRST rupee/dollar as fast as possible, working alone, "
    "part-time, with almost no capital.\n\n"
    "The input may be a product idea, a business model, a service offer, someone's "
    "report of what worked for them, or a complaint that reveals demand. Treat all of "
    "them the same: what could this person SELL, to WHOM, for HOW MUCH, and how fast?\n\n"
    "Be brutally practical. A boring service that can be sold this week beats a clever "
    "product that needs an audience. Judge only what the input supports; if a number is "
    "invented, say so in the reason.\n\n"
    "Reply with ONLY compact JSON, no markdown fences:\n"
    '{"earn_verdict":"GO|LATER|NO",'
    '"what":"what exactly to sell, in one line, concrete enough to write an offer",'
    '"buyer":"who pays, named as specifically as possible (role, business type, where to find them)",'
    '"offer":"the exact first offer and price in INR and USD",'
    '"first_payment":"realistic days to first paid rupee, integer",'
    '"capital":dollars needed to start, integer, 0 if none,'
    '"proof":"does the input contain evidence this already earns money? yes|partial|no",'
    '"reason":"one line: why this is or is not a fast first rupee for this person"}\n\n'
    "GO = a stranger could plausibly pay within 14 days. "
    "LATER = real money but needs an audience, credentials, or stock first. "
    "NO = too slow, too regulated, needs a team, or the input is just chat with no offer.")


def _money_call(title, meta, source="", body="", pain=None):
    """First-currency evaluation. Returns (dict, model).

    When `pain` (a verified row from `pains`) or `body` is supplied, pass the
    buyer's actual persona, job, workaround, cost/pay signals and verbatim quote
    instead of guessing from a title alone.
    """
    lines = [f"Opportunity: {title}", f"Source: {source}"]
    if meta:
        lines.append(f"Context: {meta}")
    if pain:
        for label, key in (
            ("Buyer persona", "persona"),
            ("Job to be done", "job"),
            ("Current workaround", "workaround"),
            ("Named tools", "tools"),
            ("Cost of problem", "cost_signal"),
            ("Willingness-to-pay signal", "pay_signal"),
            ("Verified quote", "quote"),
        ):
            val = pain.get(key)
            if isinstance(val, list):
                val = ", ".join(str(x) for x in val if x)
            elif isinstance(val, str) and key == "tools" and val.startswith("["):
                try:
                    val = ", ".join(str(x) for x in json.loads(val) if x)
                except ValueError:
                    pass
            if val:
                lines.append(f"{label}: {val}")
    if body:
        lines.append(f"Post excerpt: {str(body).strip()[:1500]}")
    prompt = "\n".join(lines) + "\n\nWhat could this person sell, to whom, for how much, and how fast?"
    txt, model = _or_chat(MONEY_SYSTEM, prompt, max_tokens=4000, timeout=120, want_json=True)
    d = parse_json(txt)
    if not d:
        return {"earn_verdict": "ERROR", "what": "", "buyer": "", "offer": "",
                "first_payment": 0, "capital": 0, "proof": "",
                "reason": f"eval failed: {model}"}, model
    v = str(d.get("earn_verdict", "LATER")).upper().strip()
    if v not in ("GO", "LATER", "NO"):
        v = "LATER"
    days = _int(d.get("first_payment"))
    # a NO verdict has no payment horizon; a bogus 999 skews "quickest money" sorting
    if v == "NO" or days > 365:
        days = 0
    what = str(d.get("what") or "")[:220]
    proof = str(d.get("proof") or "")[:20].lower()

    # Consistency guard: a GO must name something sellable. Models occasionally
    # reply GO while writing "no concrete service identified" — that is a LATER
    # at best, and left alone it puts dead rows at the top of the board.
    vague = (not what.strip()
             or what.lower().startswith(("no concrete", "nothing", "no micro",
                                         "no specific", "none ", "n/a"))
             or re.match(r"^no\s+\w*\s*(product|service|offer|idea)\b", what.lower())
             or "not a paid opportunity" in what.lower())
    if v == "GO" and vague:
        v = "LATER"
        if not d.get("reason"):
            d["reason"] = "no concrete offer could be named — not a first-currency path yet"
    # GO with zero proof and no stated price is a guess, not a lead
    if v == "GO" and proof == "no" and not str(d.get("offer") or "").strip():
        v = "LATER"

    return {
        "earn_verdict": v,
        "what": what,
        "buyer": str(d.get("buyer") or "")[:220],
        "offer": str(d.get("offer") or "")[:220],
        "first_payment": days,
        "capital": _int(d.get("capital")),
        "proof": proof,
        "reason": str(d.get("reason") or "")[:220],
    }, model


def evaluate_money(limit=20, sources=None):
    """Evaluate opportunities for the first-currency question."""
    import pain_extract
    conn = db()
    pain_extract.ensure_table(conn)
    conn.row_factory = sqlite3.Row
    where = ("(i.earn_verdict IS NULL OR i.earn_verdict='ERROR')"
             " AND (p.status IS NULL OR p.status='PAIN')")
    params = []
    if sources:
        marks = ",".join("?" * len(sources))
        where += f" AND (i.source IN ({marks}) OR p.status='PAIN')"
        params.extend(sources)
    rows = conn.execute(
        f"""SELECT i.id, i.title, i.meta, i.source, i.body,
                   p.status AS pain_status, p.persona, p.job, p.workaround,
                   p.tools, p.cost_signal, p.pay_signal, p.quote
            FROM items i LEFT JOIN pains p ON p.item_id = i.id
            WHERE {where}
            ORDER BY CASE WHEN p.status='PAIN' THEN 0 ELSE 1 END,
                     i.verdict IS NULL, i.score DESC, i.eng DESC LIMIT ?""",
        params + [limit]).fetchall()
    conn.close()
    used = {}
    for r in rows:
        pain = None
        if r["pain_status"] == "PAIN":
            pain = {k: r[k] for k in ("persona", "job", "workaround", "tools",
                                      "cost_signal", "pay_signal", "quote")}
        d, model = _money_call(r["title"], r["meta"], r["source"],
                               body=r["body"] or "", pain=pain)
        used[model] = used.get(model, 0) + 1
        c = db()
        c.execute("""UPDATE items SET earn_verdict=?, what=COALESCE(NULLIF(what,''), ?),
                     reason=?, first_payment=?, capital=?, proof=?, buyer=?, offer_money=? WHERE id=?""",
                  (d["earn_verdict"], d["what"], d["reason"], d["first_payment"],
                   d["capital"], d.get("proof", ""), d.get("buyer", ""),
                   d.get("offer", ""), r["id"]))
        c.commit(); c.close()
        time.sleep(0.6)
    return len(rows), used


from llm import chat as _llm_chat, parse_json


def _or_chat(system, user, max_tokens=3000, timeout=90, want_json=False):
    return _llm_chat(system, user, max_tokens=max_tokens, timeout=timeout, want_json=want_json)


def _int(v):
    m = re.search(r"\d+", str(v or ""))
    return int(m.group(0)) if m else 0


def _eval_call(title, meta):
    txt, model = _or_chat(EVAL_SYSTEM, f"Idea: {title}\nContext: {meta}",
                          max_tokens=3000, want_json=True)
    d = parse_json(txt)
    if not d:
        # ERROR (not SKIP) so a dead model never buries an idea; it is retried next run
        return {"verdict": "ERROR", "buildability": 0, "ttm_days": 0,
                "what": "", "reason": f"eval failed: {model}"}, "none"
    d["verdict"] = str(d.get("verdict", "WATCH")).upper().strip()
    if d["verdict"] not in ("BUILD", "WATCH", "SKIP"):
        d["verdict"] = "WATCH"
    d["buildability"] = min(10, _int(d.get("buildability")))
    d["ttm_days"] = _int(d.get("ttm_days"))
    d["what"] = str(d.get("what") or "")
    d["reason"] = str(d.get("reason") or "")
    return d, model


def evaluate_items(limit=25):
    conn = db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""SELECT id, title, meta FROM items
        WHERE verdict IS NULL OR verdict='ERROR'
        ORDER BY verdict IS NOT NULL, score DESC, eng DESC LIMIT ?""", (limit,)).fetchall()
    conn.close()
    used = {}
    for r in rows:
        d, model = _eval_call(r["title"], r["meta"])
        used[model] = used.get(model, 0) + 1
        conn = db()
        conn.execute("""UPDATE items SET verdict=?, buildability=?, ttm_days=?, what=?, reason=?
                        WHERE id=?""",
                     (d["verdict"], d["buildability"], d["ttm_days"],
                      d.get("what", "")[:200], d.get("reason", "")[:200], r["id"]))
        conn.commit(); conn.close()
        time.sleep(0.8)
    return len(rows), used


# Only sources where someone described a problem. Launch feeds (Show HN, Product
# Hunt, GitHub Trending, dev.to) announce things that already exist, so
# simulating reactions to them is meaningless.
SIMULATABLE_SOURCES = ("Reddit business ideas", "Ask HN ideas", "SomebodyMakeThis")
SIM_MAX_ATTEMPTS = 2


def pending_simulations(limit=3):
    """Ideas that should get a reaction simulation, best first.

    Excludes items already simulated, items at the attempt cap, and sources that
    are launch announcements rather than described problems.
    """
    conn = db()
    conn.row_factory = sqlite3.Row
    marks = ",".join("?" * len(SIMULATABLE_SOURCES))
    rows = conn.execute(
        """SELECT id, title, meta, source, verdict FROM items
           WHERE verdict='BUILD'
             AND source IN (%s)
             AND COALESCE(sim_attempts, 0) < ?
             AND (sim_path IS NULL OR sim_path='')
           ORDER BY COALESCE(buildability, 0) DESC, COALESCE(score, 0) DESC
           LIMIT ?""" % marks,
        tuple(SIMULATABLE_SOURCES) + (SIM_MAX_ATTEMPTS, limit)).fetchall()
    conn.close()
    return rows


def run_simulations(limit=3, max_seconds=1800):
    """Simulate reaction reports for eligible ideas. Safe to run from cron.

    Bounded by wall-clock so an unattended run cannot overrun; failures are
    recorded with a bounded attempt counter so a hopeless item is not retried
    forever. Returns a summary dict.
    """
    started = time.time()
    summary = {"attempted": 0, "done": 0, "failed": 0, "skipped": 0, "error": ""}

    ok, hint = mirofish_bridge.preflight()
    if not ok:
        summary["error"] = hint
        return summary

    rows = pending_simulations(limit=limit)
    if not rows:
        return summary

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    for row in rows:
        if time.time() - started >= max_seconds:
            summary["skipped"] += 1
            continue
        item_id = row["id"]
        out_path = EVAL_DIR / ("sim_%d.md" % item_id)
        evaluation_md = ""
        try:
            conn0 = db()
            ev = conn0.execute("SELECT report_path FROM items WHERE id=?", (item_id,)).fetchone()
            conn0.close()
            if ev and ev[0]:
                evaluation_md = Path(ev[0]).read_text()[:1500]
        except OSError:
            evaluation_md = ""
        summary["attempted"] += 1
        try:
            result = mirofish_bridge.run_simulation(
                row["title"], row["meta"], row["source"],
                out_path=out_path, evaluation=evaluation_md)
            text = mirofish_bridge.read_report(result.get("report_path") or str(out_path))
            if not text:
                raise mirofish_bridge.BridgeError("report was empty or cut off")
            conn = db()
            conn.execute(
                "UPDATE items SET sim_path=?, sim_attempts=COALESCE(sim_attempts,0)+1, sim_error=NULL WHERE id=?",
                (str(out_path), item_id))
            conn.commit(); conn.close()
            summary["done"] += 1
            print("[%s] simulated item %d -> %s" % (datetime.now(), item_id, out_path))
        except Exception as exc:
            message = str(exc)[:300]
            conn = db()
            conn.execute(
                "UPDATE items SET sim_attempts=COALESCE(sim_attempts,0)+1, sim_error=? WHERE id=?",
                (message, item_id))
            conn.commit(); conn.close()
            summary["failed"] += 1
            print("[%s] simulation failed for item %d: %s" % (datetime.now(), item_id, message))
    return summary


DEEP_SYSTEM = ("You are a senior startup evaluator running a full workup for a solo dev in India "
               "(fast cash, micro-product). Be concrete and ruthless. Markdown output with these "
               "sections: ## Verdict (BUILD/WATCH/SKIP + score /10 + one-line)\n"
               "## Pain & evidence\n## Target buyer (who pays, where they hang out)\n"
               "## Competition & wedge (3 alternatives + how this differs)\n"
               "## Monetization (pricing in INR + USD, expected first-month revenue range)\n"
               "## Distribution (exact 3 channels)\n## Risks & kill-criteria\n"
               "## 7-day MVP plan (day-by-day)\n## Final call (2 sentences).\n"
               "Keep tables to at most 4 columns with short cells. Write for a reader new to "
               "the topic: plain words, no unexplained acronyms. Total under 900 words so the "
               "Final call section is always reached.")


def deep_eval(item_id, force=False, progress=None):
    """Full workup. progress(step, label) is called at each real phase so a UI can show progress."""
    say = progress or (lambda step, label: None)
    conn = db(); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if not row:
        return None, "item not found"
    if row["report_path"] and not force and Path(row["report_path"]).exists():
        if _report_complete(Path(row["report_path"]).read_text()):
            say(2, "report already exists")
            return row["report_path"], "existing"
    prompt = f"Idea: {row['title']}\nContext: {row['meta']}\nSource: {row['source']}"
    txt, used_model = "", ""
    # hidden reasoning shares max_tokens on free models, so budget big and retry once if cut short
    for attempt in range(2):
        say(attempt, f"model writing the report (attempt {attempt + 1} of 2) · ~40-90s each")
        txt, used_model = _or_chat(DEEP_SYSTEM, prompt, max_tokens=6000, timeout=180)
        if txt and _report_complete(txt):
            break
    if not txt:
        say(2, f"failed — {used_model}")
        return None, f"failed: {used_model}"
    say(2, "saving report")
    path = BASE_DIR / "evaluations" / f"eval_{item_id}.md"
    path.parent.mkdir(exist_ok=True)
    header = (f"# Deep Evaluation — {row['title']}\n\n"
              f"*Source: {row['source']} · {row['meta']} · evaluated {datetime.now():%Y-%m-%d %H:%M} · model: {used_model}*\n\n---\n\n")
    path.write_text(header + txt)
    conn = db()
    conn.execute("UPDATE items SET report_path=? WHERE id=?", (str(path), item_id))
    conn.commit(); conn.close()
    return str(path), "new"


BASE_DIR = Path(__file__).parent
EVAL_DIR = BASE_DIR / "evaluations"


def demo_link():
    """Quick-tunnel URLs rotate on every restart, so the link lives in config, not code."""
    try:
        return json.loads((BASE / "sources_config.json").read_text()).get("demo_link", "").strip()
    except (OSError, ValueError):
        return ""


TEMPLATES = {
    "hinglish_dental": ("Hinglish - clinic DM",
        "Namaste {name} ji! Main businesses ke liye AI receptionist banata hoon - aapka patient raat ko 11 baje bhi WhatsApp kare, turant professional reply jaata hai, aur unka naam, problem, budget subah aapko Hot-leads list mein mil jaata hai.\n\n"
        "Khud try kariye - abhi live hai: {link}\n\nSetup 5000, monthly 2500. Pehle 7 din free - pasand aaye toh hi aage. Ek chhoti call kar lein?"),
    "english_dental": ("English - clinic DM",
        "Hi {name}, I saw {business} - great reviews! I build AI receptionists that reply to your patients on WhatsApp instantly - even at 11pm - qualify them and give you a Hot-lead list every morning.\n\n"
        "It is live right now - talk to it yourself: {link}\n\nSetup 5000 + 2500/month. First 7 days free. Worth 10 minutes on a call?"),
    "followup": ("Gentle follow-up (day 3)",
        "{name} ji, quick follow-up - no pressure. Last week I installed this for a clinic and they stopped losing after-hours inquiries entirely. If timing is wrong, just reply later and I will not ping again.\n\n{link}"),
    "idea_pitch": ("Pitch a productized idea",
        "Hi {name}, I am building {idea} - early birds get it at {price}. First 3 customers get lifetime updates. Interested? 2-min demo: {link}"),
    "pain_reply": ("Reddit pain reply / DM",
        "Hi {name}, saw your post about {idea}. I've been helping {business} cut out this exact manual step.\n\n"
        "Happy to do a quick done-for-you pass on one real example first so you can see if it actually saves time — no upfront pitch. Want to try it?"),
}

STATUSES = ["new", "contacted", "replied", "call", "won", "dead"]
STAGES = STATUSES[:-1]  # the pipeline in order; "dead" can happen at any stage
STATUS_LABELS = {"new": "New", "contacted": "Contacted", "replied": "Replied", "call": "On a call",
                 "won": "Won", "dead": "Dead"}


def out_db():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS prospects (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT,
        name TEXT, business TEXT, phone TEXT, channel TEXT DEFAULT 'whatsapp',
        status TEXT DEFAULT 'new', template TEXT DEFAULT 'hinglish_dental',
        last_contact TEXT, followup TEXT, notes TEXT)""")
    return conn


def seed_from_csv():
    conn = out_db()
    n = conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
    if n > 0:
        conn.close(); return
    import csv as _csv
    path = Path.home() / "Project/Project_evaluators/track_a_cash/prospects.csv"
    if not path.exists():
        conn.close(); return
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for row in _csv.DictReader(open(path)):
        if not row.get("clinic_name") or row.get("clinic_name", "").startswith("("):
            continue
        conn.execute("INSERT INTO prospects (created,name,business,phone,status,template,notes) VALUES (?,?,?,?,?,?,?)",
                     (now, row.get("contact_person") or "", row["clinic_name"], row.get("phone", ""),
                      "new", "hinglish_dental", "area: " + row.get("area", "")[:40]))
    conn.commit(); conn.close()


def _outreach_page(name="", status="all", prefill=0):
    seed_from_csv()
    conn = out_db(); conn.row_factory = sqlite3.Row
    if status in STATUSES:
        rows = conn.execute("SELECT * FROM prospects WHERE status=? ORDER BY id DESC", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM prospects ORDER BY id DESC").fetchall()
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status").fetchall())
    conn.close()

    def esc(x):
        return (x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    from urllib.parse import quote
    due = datetime.now().strftime("%Y-%m-%d")
    prefill_js = ""
    if prefill:
        ic = db(); ic.row_factory = sqlite3.Row
        idea = ic.execute("SELECT title, url, pitch, author FROM items WHERE id=?", (prefill,)).fetchone()
        pain_row = None
        if idea:
            has_pains = ic.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pains'").fetchone()
            if has_pains:
                pain_row = ic.execute(
                    "SELECT status, persona, job, quote FROM pains WHERE item_id=?", (prefill,)).fetchone()
        ic.close()
        if idea:
            if pain_row and pain_row["status"] == "PAIN":
                author = (idea["author"] or "").strip()
                lead_name = f"u/{author}" if author and author not in ("[deleted]", "[removed]") else ""
                parts = []
                if pain_row["job"]:
                    parts.append("Job: " + pain_row["job"])
                if pain_row["quote"]:
                    parts.append('Quote: "' + pain_row["quote"] + '"')
                if idea["url"]:
                    parts.append("Thread: " + idea["url"])
                payload = {
                    "template": "pain_reply",
                    "name": lead_name,
                    "business": (pain_row["persona"] or "")[:120],
                    "notes": " | ".join(parts),
                    "idea": (pain_row["job"] or idea["title"] or "")[:80],
                }
            else:
                ptitle = (idea["title"] or "")[:120]
                notes = idea["pitch"] if idea["pitch"] else f"Pitch: {ptitle} (source: {idea['url']})"
                payload = {
                    "template": "idea_pitch",
                    "name": "",
                    "business": ptitle[:80],
                    "notes": notes,
                    "idea": ptitle[:80],
                }
            prefill_js = "<script>window.__PREFILL__=" + json.dumps(payload).replace("</", "<\\/") + ";</script>"
    cards = ""
    for r in rows:
        tname, ttext = TEMPLATES.get(r["template"], list(TEMPLATES.values())[0])
        notes_raw = r["notes"] or ""
        if notes_raw.startswith("Job: "):
            idea_txt = notes_raw[5:].split(" | ", 1)[0]
        elif notes_raw.startswith("Pitch: "):
            idea_txt = notes_raw.replace("Pitch: ", "", 1)
        else:
            idea_txt = "a new micro-product"
        msg = (ttext.replace("{name}", r["name"] or "there").replace("{business}", r["business"] or "your business")
                .replace("{link}", demo_link() or "(demo link coming)")
                .replace("{idea}", idea_txt[:90]).replace("{price}", "999"))
        phone = re.sub(r"[^0-9]", "", r["phone"] or "")
        if phone and not phone.startswith("91"):
            phone = "91" + phone
        phone = phone if len(phone) >= 10 else ""
        wa = ("https://wa.me/" + phone + "?text=" + quote(msg)) if phone else ""
        thread_m = re.search(r"https?://[^\s|)\"'>]+", notes_raw)
        is_due = bool(r["followup"] and r["followup"] <= due and r["status"] not in ("won", "dead"))
        due_line = ('<p class="due">Follow up today</p>' if is_due else
                    ('<p class="when">Follow up on ' + esc(r["followup"]) + '</p>' if r["followup"] else ""))
        if phone:
            wa_btn = '<a class="btn" href="' + esc(wa) + '" target="_blank" rel="noopener">Open in WhatsApp</a>'
        elif thread_m:
            wa_btn = '<a class="btn" href="' + esc(thread_m.group(0)) + '" target="_blank" rel="noopener">Open thread</a>'
        else:
            wa_btn = '<span class="nophone">No phone number yet</span>'
        cur = STAGES.index(r["status"]) if r["status"] in STAGES else -1
        stages = "".join(
            '<li class="' + ("past" if i < cur else ("now" if i == cur else "")) + '">'
            '<a href="/outreach/status/' + str(r["id"]) + '/' + st + '"'
            + (' aria-current="step"' if i == cur else '') + '>' + STATUS_LABELS[st] + '</a></li>'
            for i, st in enumerate(STAGES))
        dead = ('<a class="deadlink" href="/outreach/status/' + str(r["id"]) + '/dead"'
                + (' aria-current="true">Marked dead' if r["status"] == "dead" else '>Mark dead') + '</a>')
        cards += ('<article class="prospect st-' + esc(r["status"]) + '">'
                  + '<h2>' + esc(r["name"] or r["business"] or "Unnamed prospect") + '</h2>'
                  + ('<p class="biz">' + esc(r["business"]) + '</p>' if r["name"] and r["business"] else '')
                  + due_line
                  + ('<p class="notes">' + esc(r["notes"]) + '</p>' if r["notes"] else '')
                  + '<ol class="stages" aria-label="Stage">' + stages + '</ol>' + dead
                  + '<details><summary>Show message, ' + esc(tname) + '</summary>'
                  + '<pre class="omsg">' + esc(msg) + '</pre></details>'
                  + '<div class="oacts">' + wa_btn
                  + '<button type="button" class="copy" onclick="copyMsg(this)" data-msg="' + esc(msg)
                  + '">Copy message</button></div></article>')
    if not cards:
        cards = ('<div class="empty"><p>Nobody here yet.</p>'
                 '<p>Add a prospect above, or use <b>Draft pitch</b> on an idea and send it here.</p></div>')

    total = sum(counts.values())
    due_n = sum(1 for r in rows if r["followup"] and r["followup"] <= due
                and r["status"] not in ("won", "dead")) if status not in STATUSES else None
    if due_n is None:
        conn2 = out_db()
        due_n = conn2.execute("SELECT COUNT(*) FROM prospects WHERE followup IS NOT NULL AND followup != ''"
                              " AND followup <= ? AND status NOT IN ('won','dead')", (due,)).fetchone()[0]
        conn2.close()
    answered = sum(counts.get(s, 0) for s in ("replied", "call", "won"))
    lede = ("%d %s in the pipeline. %d to follow up today; %d replied so far."
            % (total, "person" if total == 1 else "people", due_n, answered))
    tabs = ('<a class="tab" href="/outreach"' + (' aria-current="page"' if status not in STATUSES else '')
            + '>All (' + str(total) + ')</a>' + "".join(
                '<a class="tab" href="/outreach?status=' + st + '"'
                + (' aria-current="page"' if status == st else '') + '>' + STATUS_LABELS[st]
                + ' (' + str(counts.get(st, 0)) + ')</a>' for st in STATUSES))
    import theme
    html = (BASE / "outreach.html").read_text()
    return (html.replace("{{FONTS}}", theme.FONTS).replace("{{THEME_CSS}}", theme.CSS)
                .replace("{{HEADER}}", theme.header("outreach", "Outreach"))
                .replace("{{LEDE}}", esc(lede)).replace("{{TABS}}", tabs)
                .replace("{{FORM_OPEN}}", " open" if prefill or not total else "")
                .replace("{{CARDS}}", cards)
                .replace("{{PREFILL_JS}}", prefill_js or ""))


app = FastAPI(title="IdeaPulse")

# Visitors through a shared Cloudflare link must enter an access code; local use is untouched.
import share_gate
share_gate.install(app)

# One MiroFish simulation at a time (see the /simulate route for why).
_SIM_LOCK = threading.Lock()
_SIM_RUNNING = {"id": None}

from launch import router as launch_router
app.include_router(launch_router)

from money import router as money_router
app.include_router(money_router)

from pains_page import router as pains_router
app.include_router(pains_router)

from sim_guide import router as sim_guide_router
app.include_router(sim_guide_router)


@app.get("/jobs/{jid}")
def job_status(jid: str):
    import jobs
    j = jobs.get(jid)
    return j if j else {"state": "missing"}


@app.get("/outreach", response_class=HTMLResponse)
def outreach(status: str = "all", prefill: int = 0):
    return _outreach_page(status=status, prefill=prefill)


@app.get("/outreach/status/{pid}/{st}")
def out_status(pid: int, st: str):
    if st in STATUSES:
        conn = out_db()
        conn.execute("UPDATE prospects SET status=?, last_contact=? WHERE id=?",
                     (st, datetime.now().strftime("%Y-%m-%d %H:%M"), pid))
        conn.commit(); conn.close()
    return RedirectResponse("/outreach", status_code=303)


async def _form_fields(request):
    """A urlencoded form as {name: value}.

    Not request.form(): that needs python-multipart, which the server's Python
    (/usr/bin/python3, run by launchd) does not have, so every form post failed
    with a 500 there while passing in the test venv.
    """
    from urllib.parse import parse_qs
    body = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


@app.post("/outreach/add")
async def outreach_add(request: Request):
    f = await _form_fields(request)
    conn = out_db()
    conn.execute("INSERT INTO prospects (created,name,business,phone,status,template,followup,notes) VALUES (?,?,?,?,?,?,?,?)",
                 (datetime.now().strftime("%Y-%m-%d %H:%M"), f.get("name", ""), f.get("business", ""),
                  f.get("phone", ""), "new", f.get("template", "hinglish_dental"),
                  f.get("followup") or None, f.get("notes", "")))
    conn.commit(); conn.close()
    return RedirectResponse("/outreach", status_code=303)

NUTSHELL_SYSTEM = ("Explain the given app/product idea in simple plain English (like to a smart 12-year-old). "
                   "Max 3 short sentences covering: what it does, who would pay for it, why it's trending. "
                   "No jargon, no markdown, plain text only.")


@app.post("/explain/{item_id}")
@app.get("/explain/{item_id}")
def explain(item_id: int):
    conn = db(); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT title, meta, source, nutshell FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if not row:
        return {"nutshell": "not found"}
    if row["nutshell"]:
        return {"nutshell": row["nutshell"]}
    txt, model = _or_chat(NUTSHELL_SYSTEM,
        f"Idea: {row['title']} (from {row['source']}) — {row['meta']}", max_tokens=900, timeout=90)
    txt = txt.replace("\n", " ").strip() if txt else ""
    if txt:
        conn = db()
        conn.execute("UPDATE items SET nutshell=? WHERE id=?", (txt[:400], item_id))
        conn.commit(); conn.close()
    return {"nutshell": txt or f"explanation failed — {model}"}


PITCH_SYSTEM = ("You write cold WhatsApp outreach messages for a solo developer in India selling "
                "micro-products for fast cash. Write ONE ready-to-send WhatsApp message. Casual-warm, "
                "plain text, no markdown, no subject lines. The message MUST contain, in order: "
                "(1) 1-line hook about the pain, (2) 2 lines on what the product does and the concrete "
                "outcome, (3) a price line in INR (pick a sensible amount for the idea, or early-bird "
                "pricing if unclear), (4) a soft question CTA, (5) the demo link placeholder {link} alone "
                "on the final line. Keep it under 90 words. Output ONLY the message text.")


@app.post("/pitch/{item_id}")
@app.get("/pitch/{item_id}")
def pitch(item_id: int):
    conn = db(); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT title, meta, source, what, pitch FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if not row:
        return {"pitch": "", "status": "item not found"}
    if row["pitch"]:
        return {"pitch": row["pitch"], "status": "cached"}
    txt, model = _or_chat(PITCH_SYSTEM,
        f"Idea: {row['title']}\nWhat to build: {row['what'] or '(not evaluated yet)'}\n"
        f"Context: {row['meta']}\nSource: {row['source']}",
        max_tokens=1500, timeout=120)
    if not txt:
        return {"pitch": "", "status": f"failed: {model}"}
    import humanizer
    hum = humanizer.humanize_reply(txt.strip()[:900], rewrite_fn=humanizer._default_rewrite)
    txt = hum["text"][:900]
    conn = db()
    conn.execute("UPDATE items SET pitch=? WHERE id=?", (txt, item_id))
    conn.commit(); conn.close()
    return {"pitch": txt, "status": "new", "model": model,
            "human_score": hum["human_score"], "human_verdict": hum["verdict"]}


@app.post("/deepeval/{item_id}")
@app.get("/deepeval/{item_id}")
def deepeval(item_id: int, force: int = 0, async_: int = 0):
    """Deep evaluate one idea. async_=1 returns a job id so the page can show progress."""
    if async_:
        import jobs
        conn = db(); conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT title FROM items WHERE id=?", (item_id,)).fetchone()
        conn.close()
        title = (row["title"] if row else f"#{item_id}")[:70]

        def work(tick, label):
            seen = {"step": 0}

            def progress(step, msg):
                label(msg)
                if step > seen["step"]:
                    tick(step - seen["step"])
                    seen["step"] = step

            path, status = deep_eval(item_id, force=bool(force), progress=progress)
            label("report saved" if status in ("new", "existing") else f"failed — {status}")
            # a failed report must surface as a red bar, not a green tick
            if status not in ("new", "existing"):
                raise RuntimeError(status)
            return {"path": path, "status": status}

        return {"job": jobs.start(f"Deep evaluating “{title}”", 2, work,
                                  expect_s=(40, 180))}
    path, status = deep_eval(item_id, force=bool(force))
    return {"path": path, "status": status}


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

    # Check the backend before taking the lock or spawning anything. With a
    # placeholder ZEP key the graph build blocks at 55% for minutes, so an
    # immediate, actionable error beats a job that looks busy then dies.
    ok, hint = mirofish_bridge.preflight()
    if not ok:
        return {"ok": False, "error": hint}

    # One simulation at a time. MiroFish holds a single Kuzu graph handle per
    # process and a run costs 6-11 minutes of model calls; two concurrent runs
    # contend for that handle and both get slower (observed: three parallel
    # builds, none finishing). Refuse rather than queue.
    with _SIM_LOCK:
        if _SIM_RUNNING["id"]:
            return {"ok": False,
                    "error": "a simulation is already running for idea %s — wait for it "
                             "to finish (MiroFish handles one run at a time)"
                             % _SIM_RUNNING["id"],
                    "busy": True}
        _SIM_RUNNING["id"] = item_id

    # EVAL_DIR, not a path built here: tests point EVAL_DIR at a temp folder, and a
    # hard-coded path made the suite write sim_<id>.md into the repo tree.
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVAL_DIR / ("sim_%d.md" % item_id)
    evaluation_md = ""
    if row["report_path"]:
        try:
            evaluation_md = Path(row["report_path"]).read_text()[:1500]
        except OSError:
            evaluation_md = ""

    def work(tick, label):
        try:
            return _run_sim_work(tick, label)
        finally:
            with _SIM_LOCK:
                _SIM_RUNNING["id"] = None

    def _run_sim_work(tick, label):
        def on_stage(pct, msg):
            # The bridge reports 0/30/55/85 across its four stages; the job is
            # counted out of 4 steps. Map that percentage onto the step counter so
            # the bar moves during a 6-minute run instead of sitting at 0/4.
            label(msg)
            want = int(round(pct / 100.0 * 4))
            if want > STEP_SEEN["n"]:
                tick(want - STEP_SEEN["n"])
                STEP_SEEN["n"] = want
        STEP_SEEN = {"n": 0}
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
        # 6-11 minutes measured (design doc §5); stages are uneven, so the
        # per-step rate alone would mislead early on.
        return {"job": jobs.start("Simulating %s" % title, 4, work,
                                  expect_s=(360, 660))}
    try:
        result = work(lambda n: None, lambda msg: None)
        return {"ok": True, "status": "new", "path": result["path"]}
    except mirofish_bridge.BridgeError as e:
        return {"ok": False, "error": str(e)}


def _inline(text):
    import html as _html
    e = _html.escape(text.strip())
    codes = []
    def keep(m):
        codes.append("<code>" + m.group(1) + "</code>")
        return f"\x01{len(codes) - 1}\x01"
    e = re.sub(r"`([^`]+)`", keep, e)
    e = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank">\1</a>', e)
    e = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", e)
    e = re.sub(r"(?<![*\w])\*([^*]+)\*(?![*\w])", r"<i>\1</i>", e)
    e = re.sub(r"(?<![_\w])_([^_]+)_(?![_\w])", r"<i>\1</i>", e)
    for i, c in enumerate(codes):
        e = e.replace(f"\x01{i}\x01", c)
    return e


def _table(lines):
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]
    rows = [r for r in rows if not all(re.fullmatch(r":?-{2,}:?", c) for c in r if c)]
    if not rows:
        return ""
    head, body = rows[0], rows[1:]
    th = "".join(f"<th>{_inline(c)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f'<td data-label="{_inline(re.sub(r"[*`]", "", head[i]) if i < len(head) else "")}">{_inline(c)}</td>'
                                   for i, c in enumerate(r)) + "</tr>" for r in body)
    return f'<div class="tbl"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def _md_to_html(md):
    """Markdown → HTML for model reports: headings, tables, lists, quotes, code, links."""
    import html as _html
    blocks = []
    def stash(m):
        blocks.append('<pre class="code">' + _html.escape(m.group(1)) + "</pre>")
        return f"\x00{len(blocks) - 1}\x00"
    md = re.sub(r"```[^\n]*\n(.*?)```", stash, md, flags=re.S)
    lines = md.split("\n")
    out, lst = [], None
    def close_list():
        nonlocal lst
        if lst:
            out.append(f"</{lst}>"); lst = None
    def open_list(kind):
        nonlocal lst
        if lst != kind:
            close_list(); out.append(f"<{kind}>"); lst = kind
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("|"):
            close_list()
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            out.append(_table(lines[i:j])); i = j; continue
        i += 1
        if not s:
            close_list(); continue
        h = re.match(r"^(#{1,4})\s+(.*)", s)
        if h:
            close_list(); n = min(len(h.group(1)), 3)
            out.append(f"<h{n}>{_inline(h.group(2))}</h{n}>")
        elif re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", s):
            close_list(); out.append("<hr>")
        elif s.startswith(">"):
            close_list(); out.append("<blockquote>" + _inline(s.lstrip("> ")) + "</blockquote>")
        elif re.match(r"^[-*•]\s+", s):
            open_list("ul"); out.append("<li>" + _inline(re.sub(r"^[-*•]\s+", "", s)) + "</li>")
        elif re.match(r"^\d+[.)]\s+", s):
            open_list("ol"); out.append("<li>" + _inline(re.sub(r"^\d+[.)]\s+", "", s)) + "</li>")
        elif re.fullmatch(r"\x00\d+\x00", s):
            close_list(); out.append(s)
        else:
            close_list(); out.append("<p>" + _inline(s) + "</p>")
    close_list()
    html = "\n".join(out)
    for k, b in enumerate(blocks):
        html = html.replace(f"\x00{k}\x00", b)
    return html


def _report_complete(md):
    return (bool(re.search(r"^#+\s*Final call", md, re.I | re.M))
            and bool(re.search(r"[.!?][*_\"')\]]*$", md.rstrip())))


REPORT_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
{{FONTS}}
<style>
{{THEME_CSS}}
.doc{max-width:68ch;margin-top:40px;font-size:17px;line-height:1.7}
.doc h1{font:500 38px/1.15 var(--serif);letter-spacing:-.01em;margin:0 0 10px;text-wrap:balance}
.doc h2{font:500 26px/1.25 var(--serif);margin:44px 0 12px}
.doc h3{font-size:17px;font-weight:700;margin:26px 0 8px}
.doc p{margin:0 0 14px}
.doc ul,.doc ol{margin:0 0 14px 24px}
.doc li{margin:6px 0}
.doc b,.doc strong{color:#fff}
.doc a{color:var(--ink);text-decoration-color:var(--faint);text-underline-offset:3px}
.doc blockquote{font:italic 400 18px/1.6 var(--serif);color:var(--ink);border-left:2px solid var(--rule);
  margin:16px 0;padding:2px 0 2px 18px}
.doc hr{border:none;border-top:1px solid var(--rule);margin:30px 0}
.doc code{background:var(--surface);border-radius:4px;padding:1px 6px;font-size:15px;color:var(--ink)}
.doc .code{background:var(--surface);border-radius:6px;padding:14px 16px;overflow-x:auto;font-size:14px;white-space:pre-wrap;margin:0 0 14px}
.doc .meta{color:var(--muted);font-size:14.5px;margin:0 0 24px}
.tbl{overflow-x:auto;margin:16px 0 20px;max-width:none}
.doc table{border-collapse:collapse;width:100%;font-size:15px;line-height:1.5}
.doc th{text-align:left;color:var(--muted);font-weight:700;padding:8px 14px 8px 0;border-bottom:1px solid var(--faint)}
.doc td{padding:10px 14px 10px 0;border-bottom:1px solid var(--rule);vertical-align:top}
.warn{border-left:3px solid var(--flag);padding:6px 0 6px 14px;margin:16px 0 26px;font-size:15px}
.warn button{font:700 14px/1 var(--sans);border-radius:6px;padding:9px 14px;cursor:pointer;margin-left:10px;
  border:1px solid var(--ink);background:var(--ink);color:var(--ground)}
@media (max-width:860px){
  .doc{margin-top:26px;font-size:16px}
  .doc h1{font-size:28px}
  .doc h2{font-size:22px}
  .doc blockquote{font-size:17px}
  .tbl{border:none}
  .doc table,.doc thead,.doc tbody,.doc tr,.doc td{display:block;width:100%}
  .doc thead{display:none}
  .doc tr{border-bottom:1px solid var(--rule);padding:8px 0}
  .doc td{border:none;padding:3px 0}
  .doc td:first-child{font-weight:700}
  .doc td[data-label]:not(:first-child)::before{content:attr(data-label);display:block;color:var(--muted);font-size:13px}
}
</style></head><body>
{{TOPBAR}}
<article class="doc">{{BODY}}</article>
<script>
async function regen(id, btn){
  btn.disabled = true;
  btn.textContent = 'Starting…';
  const r = await (await fetch('/deepeval/' + id + '?force=1&async_=1')).json();
  JobBar.watch(r.job, (j) => {
    if (j.state === 'done') location.reload();
    else { btn.textContent = 'Failed — ' + (j.message || 'unknown'); btn.disabled = false; }
  });
}
</script>
{{JOBBAR}}
</body></html>"""


def _report_page(body, title, status_code=200):
    """A report (deep evaluation or simulation) in the shared look."""
    import theme
    return HTMLResponse(REPORT_PAGE.replace("{{FONTS}}", theme.FONTS).replace("{{THEME_CSS}}", theme.CSS)
                        .replace("{{TOPBAR}}", theme.topbar()).replace("{{TITLE}}", theme.esc(title))
                        .replace("{{BODY}}", body).replace("{{JOBBAR}}", __import__("jobs").WIDGET),
                        status_code=status_code)


def _missing_report(what, how):
    return _report_page(f'<h1>{what}</h1><p>{how}</p><p><a href="/">Back to Ideas</a></p>',
                        what + " — IdeaPulse", status_code=404)


@app.get("/report/{item_id}", response_class=HTMLResponse)
def report(item_id: int):
    conn = db(); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT report_path FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    md = ""
    if row and row["report_path"]:
        try:
            md = Path(row["report_path"]).read_text()
        except Exception:
            pass
    if not md:
        return _missing_report("No full report yet",
                               "Open the idea on the Ideas page and choose Deep evaluate to write one.")
    body = _md_to_html(md)
    body = re.sub(r"<p><i>(Source:.*?)</i></p>", r'<p class="meta">\1</p>', body, count=1)
    if not _report_complete(md):
        warn = (f'<div class="warn">This report stops before the end: the model ran out of space.'
                f'<button onclick="regen({item_id}, this)">Regenerate full report</button></div>')
        body = re.sub(r"(</h1>)", r"\1" + warn.replace("\\", "\\\\"), body, count=1)
    return _report_page(body, "Full report — IdeaPulse")


@app.get("/simulation/{item_id}", response_class=HTMLResponse)
def simulation_report(item_id: int):
    conn = db(); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT sim_path, title, what FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    md = mirofish_bridge.read_report(row["sim_path"]) if row and row["sim_path"] else ""
    if not md:
        return _missing_report("No simulation yet",
                               "Open the idea on the Ideas page and choose Simulate buyers. "
                               "A run takes about 6 to 11 minutes.")
    # MiroFish titles its output "Future Forecast Report" and writes in the future
    # tense, but it is a simulated reaction, not a forecast, and no time-series
    # model is involved. Retitle so the page does not overclaim.
    md = _retitle_simulation(md, (row["title"] or "") if row else "")
    body = _md_to_html(md)
    # Plain-language explainer first: what this is, who took part, what it cannot tell you.
    intro = ""
    try:
        import sim_explain
        intro = sim_explain.render(md, (row["title"] or "")[:110] if row else "",
                                   ((row["what"] or "") + " " + (row["title"] or "")) if row else "")
    except Exception:
        intro = ""
    # Heading and "for:" line first, then the plain-language explainer, then the report.
    body = re.sub(r"</h1>\s*(<p><i>Simulated reaction for:.*?</i></p>)?",
                  lambda m: m.group(0) + intro, body, count=1)
    return _report_page(body, "Simulation — IdeaPulse")


def _retitle_simulation(md, idea_title=""):
    """Replace MiroFish's 'Future Forecast Report' heading with an accurate one.

    The body still reads "will" because it is a transcript of what the simulated
    agents did; the disclaimer in the explainer covers that. Only the title is
    corrected, since it is the one line that claims forecasting.
    """
    first, _, rest = (md or "").partition("\n")
    if not first.startswith("#"):
        return md
    heading = ("# How the personas reacted\n\n"
               f"*Simulated reaction for: {idea_title}*" if idea_title
               else "# How the personas reacted")
    return heading + ("\n" + rest if rest else "")


@app.post("/fetch")
@app.get("/fetch")
def fetch(async_: int = 0):
    """Scrape all sources. async_=1 returns a job id so the page can show per-source progress."""
    if async_:
        import jobs

        def work(tick, label):
            import httpx as _hx
            total, report = 0, []
            srcs = discover_sources()
            with _hx.Client(headers=UA, timeout=15, follow_redirects=True) as client:
                for i, (name, fn) in enumerate(srcs, 1):
                    label(f"{i}/{len(srcs)} · {name}")
                    try:
                        got = fn(client)
                        got = got[0] if isinstance(got, tuple) else got
                        n = save_items(name, got)
                        set_status(name, "ok", n)
                        report.append(f"{name}: +{n}")
                        total += n
                    except Exception as e:
                        set_status(name, f"FAILED {type(e).__name__}", 0)
                        report.append(f"{name}: FAILED ({type(e).__name__})")
                    tick(1)
            label(f"{total} new items from {len(srcs)} sources")
            return {"new_items": total, "sources": report}

        n = len(discover_sources())
        return {"job": jobs.start("Fetching all sources", n, work)}
    total, report = fetch_all()
    return {"new_items": total, "sources": report}


@app.post("/evaluate")
@app.get("/evaluate")
def evaluate(limit: int = 25, async_: int = 0):
    """Evaluate pending items. async_=1 returns a job id so the page can show progress."""
    if async_:
        import jobs
        limit = max(1, min(limit, 60))

        def work(tick, label):
            used = {}
            conn2 = db(); conn2.row_factory = sqlite3.Row
            rows = conn2.execute("""SELECT id, title, meta FROM items
                WHERE verdict IS NULL OR verdict='ERROR'
                ORDER BY verdict IS NOT NULL, score DESC, eng DESC LIMIT ?""",
                (limit,)).fetchall()
            conn2.close()
            for i, r in enumerate(rows, 1):
                label(f"{i}/{len(rows)} · {r['title'][:60]}")
                d, model = _eval_call(r["title"], r["meta"])
                used[model] = used.get(model, 0) + 1
                c = db()
                c.execute("""UPDATE items SET verdict=?, buildability=?, ttm_days=?, what=?, reason=?
                             WHERE id=?""",
                          (d["verdict"], d["buildability"], d["ttm_days"],
                           d.get("what", "")[:200], d.get("reason", "")[:200], r["id"]))
                c.commit(); c.close()
                tick(1)
                time.sleep(0.8)
            label(f"evaluated {len(rows)} ideas")
            return {"evaluated": len(rows), "models": used}

        n_pending = db().execute("SELECT COUNT(*) FROM items WHERE verdict IS NULL OR verdict='ERROR'").fetchone()[0]
        total = min(limit, n_pending)
        return {"job": jobs.start(f"Evaluating {total} ideas", total, work)}
    n, used = evaluate_items(limit)
    return {"evaluated": n, "models": used}


PAGE_SIZE = 25


VERDICT_ORDER = "CASE verdict WHEN 'BUILD' THEN 0 WHEN 'WATCH' THEN 1 WHEN 'SKIP' THEN 3 ELSE 2 END"


@app.get("/", response_class=HTMLResponse)
def dashboard(page: int = 0, filter: str = "all", source: str = "all"):
    conn = db()
    conn.row_factory = sqlite3.Row
    where, params = [], []
    if filter == "build":
        where.append("verdict='BUILD'")
    elif filter == "watch":
        where.append("verdict='WATCH'")
    elif filter == "skip":
        where.append("verdict='SKIP'")
    elif filter == "live":
        where.append("(verdict='BUILD' OR verdict='WATCH')")
    elif filter == "pending":
        where.append("(verdict IS NULL OR verdict='ERROR')")
    if source != "all":
        where.append("source=?"); params.append(source)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) c FROM items {wsql}", params).fetchone()["c"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows = conn.execute(
        f"SELECT * FROM items {wsql} ORDER BY {VERDICT_ORDER}, buildability DESC, eng DESC, id DESC LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
    today = datetime.now().strftime("%Y-%m-%d")
    today_count = conn.execute(
        "SELECT COUNT(*) c FROM items WHERE fetched_on LIKE ?", (f"{today}%",)
    ).fetchone()["c"]
    source_counts = dict(conn.execute("SELECT source, COUNT(*) FROM items GROUP BY source").fetchall())
    src_status = get_statuses()
    counts = dict(conn.execute("SELECT verdict, COUNT(*) FROM items GROUP BY verdict").fetchall())
    conn.close()

    def esc(s):
        return html.escape(str(s or ""), quote=True)

    def qs(p=0, f=None, so=None):
        return "/?page=%d&filter=%s&source=%s" % (p, f or filter, quote_url(so or source))

    def plain_meta(r):
        meta = re.sub(r"(\d+)\s*↑", r"\1 upvotes", (r["meta"] or "").replace(" · ", ", "))
        meta = meta if len(meta) <= 120 else meta[:117].rstrip(" ,") + "…"
        when = (r["fetched_on"] or "")[:10]
        return ", ".join(x for x in (r["source"], meta, when and "fetched " + when) if x)

    def actions(r, verdict):
        acts = []
        if r["report_path"]:
            acts.append(f'<a href="/report/{r["id"]}" target="_blank">Full report</a>')
        else:
            acts.append(f'<button type="button" onclick="deepeval({r["id"]})">Deep evaluate</button>')
        acts.append(f'<button type="button" onclick="explainIdea({r["id"]})">Explain</button>')
        acts.append(f'<button type="button" onclick="pitchIdea({r["id"]})">Draft pitch</button>')
        acts.append(f'<button type="button" onclick="simulate({r["id"]})">Simulate buyers</button>')
        if verdict in ("BUILD", "WATCH"):
            acts.append(f'<a href="/launch?product={r["id"]}">Where to launch</a>')
        return '<div class="acts">' + "".join(acts) + "</div>"

    def score_line(r, verdict):
        b = r["buildability"] or 0
        days = r["ttm_days"]
        if days in (None, ""):
            ship = ""
        elif int(days) <= 1:
            ship = ", could ship within a day"
        else:
            ship = f", about {days} days to ship"
        word = "Build" if verdict == "BUILD" else "Watch"
        return (f'<p class="score"><span class="vw">{word}.</span> Buildable {b} of 10{ship}.'
                f'<span class="ticks" aria-hidden="true">'
                + "".join(f'<i class="{"on" if i < b else ""}"></i>' for i in range(10))
                + "</span></p>")

    entries = ""
    for r in rows:
        verdict = (r["verdict"] or "").upper()
        title = (f'<a class="tl" href="{esc(r["url"])}" target="_blank" rel="noopener">'
                 f'<mark>{esc(r["title"])}</mark></a>')
        ns = (f'<p class="ns" id="ns_{r["id"]}">{esc(r["nutshell"])}</p>' if r["nutshell"]
              else f'<p class="ns" id="ns_{r["id"]}" hidden></p>')
        pitch = f'<div class="pitchbox" id="pitch_{r["id"]}" hidden></div>'
        if verdict in ("BUILD", "WATCH"):
            what = f'<p class="what">{esc(r["what"])}</p>' if r["what"] else ""
            entries += (f'<article class="entry {verdict.lower()}"><h2>{title}</h2>{score_line(r, verdict)}'
                        f'{what}{ns}{pitch}<p class="meta">{esc(plain_meta(r))}</p>{actions(r, verdict)}</article>')
        else:
            hot = (r["score"] or 0) >= 6 or (r["eng"] or 0) >= 100
            if verdict == "SKIP":
                note = f'<span class="why">Skipped: {esc((r["reason"] or "no reason given")[:120])}</span>'
            elif verdict == "ERROR":
                note = '<span class="why flag">Evaluation failed, will retry</span>'
            else:
                note = '<span class="why">Popular, not read yet</span>' if hot else ""
            entries += (f'<article class="row {verdict.lower() or "unread"}"><h3>{title}</h3>'
                        f'<p class="meta">{note}{esc(plain_meta(r))}</p>{ns}{pitch}'
                        f'{actions(r, verdict)}</article>')
    if not rows:
        entries = ('<div class="empty"><p>Nothing matches this view.</p>'
                   f'<p><a href="{qs(0, "all", "all")}">Show every idea</a> or fetch new ones.</p></div>')

    # ---- sources rail: health and filter in one list --------------------------------
    status_by = {st["source"]: st for st in src_status}

    def health(name):
        st = status_by.get(name)
        if not st:
            return "", "not run yet"
        s = st["status"] or ""
        if s.startswith("ok"):
            return "ok", f'+{st["count"]} at {st["last_run"]}'
        if s.startswith("EMPTY"):
            return "bad", "returned nothing"
        if s.startswith("skipped"):
            return "bad", "needs " + s.split("set ", 1)[-1] if "set " in s else s
        return "bad", "failed"

    names = sorted(set(source_counts) | set(status_by), key=str.lower)
    problems = sum(1 for n in names if health(n)[0] == "bad")
    rail_items = "".join(
        f'<li class="{health(n)[0]}"><a href="{qs(0, None, n)}"{" aria-current=true" if source == n else ""}>'
        f'<span class="sn">{esc(n)}</span><span class="sc">{source_counts.get(n, 0)}</span>'
        f'<span class="sh">{esc(health(n)[1])}</span></a></li>'
        for n in names)
    rail = ('<aside class="rail"><details open id="srcs"><summary>Sources'
            + (f' <span class="probs">{problems} need attention</span>' if problems else "")
            + '</summary>'
            + (f'<a class="allsrc" href="{qs(0, None, "all")}">Show all sources</a>' if source != "all" else "")
            + f'<ul>{rail_items}</ul></details></aside>')

    # ---- verdict tabs ---------------------------------------------------------------
    vcounts = {"BUILD": counts.get("BUILD", 0), "WATCH": counts.get("WATCH", 0),
               "SKIP": counts.get("SKIP", 0),
               "pending": counts.get(None, 0) + counts.get("", 0) + counts.get("ERROR", 0)}
    tab_defs = [("build", "Build", vcounts["BUILD"]), ("watch", "Watch", vcounts["WATCH"]),
                ("live", "Build and watch", vcounts["BUILD"] + vcounts["WATCH"]),
                ("pending", "Not read yet", vcounts["pending"]), ("skip", "Skipped", vcounts["SKIP"]),
                ("all", "All", sum(counts.values()))]
    tabs = "".join(f'<a class="tab" href="{qs(0, f)}"{" aria-current=page" if filter == f else ""}>'
                   f'{label} ({n:,})</a>' for f, label, n in tab_defs)

    pager = '<nav class="pager" aria-label="Pages">'
    if page > 0:
        pager += f'<a href="{qs(page - 1)}">Newer</a>'
    pager += f'<span>Page {page + 1} of {pages}, {total:,} ideas</span>'
    if page < pages - 1:
        pager += f'<a href="{qs(page + 1)}">Older</a>'
    pager += "</nav>"

    view = ("" if source == "all"
            else f'<p class="viewing">Only ideas from <b>{esc(source)}</b>. '
                 f'<a href="{qs(0, None, "all")}">Show all sources</a></p>')
    def are(n):
        return f"{n:,} is" if n == 1 else f"{n:,} are"
    lede = (f'{today_count:,} new idea{"" if today_count == 1 else "s"} came in today. '
            f'{are(vcounts["BUILD"])} worth building and {vcounts["WATCH"]:,} worth watching; '
            f'{vcounts["pending"]:,} {"hasn’t" if vcounts["pending"] == 1 else "haven’t"} been read yet.')
    day = datetime.now().strftime("%A %-d %B")

    import theme
    return (HTML_TEMPLATE.replace("{{FONTS}}", theme.FONTS).replace("{{THEME_CSS}}", theme.CSS)
            .replace("{{HEADER}}", theme.header("ideas", "IdeaPulse"))
            .replace("{{ENTRIES}}", entries).replace("{{PAGER}}", pager)
            .replace("{{LEDE}}", esc(lede)).replace("{{DAY}}", day)
            .replace("{{TABS}}", tabs).replace("{{RAIL}}", rail).replace("{{VIEW}}", view)
            .replace("{{JOBBAR}}", __import__("jobs").WIDGET)
            .replace("{{DEMO_LINK}}", json.dumps(demo_link() or "(demo link coming)")))


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>IdeaPulse</title>
{{FONTS}}
<style>
{{THEME_CSS}}

.layout{display:grid;grid-template-columns:240px minmax(0,1fr);gap:56px;margin-top:34px}
.rail summary{font:700 13px/1.3 var(--sans);color:var(--muted);cursor:pointer;list-style:none;margin-bottom:10px}
.rail summary::-webkit-details-marker{display:none}
.probs{color:var(--flag);font-weight:400;margin-left:4px}
.rail ul{list-style:none}
.rail li a{display:grid;grid-template-columns:1fr auto;column-gap:10px;text-decoration:none;padding:7px 0;
  border-bottom:1px solid var(--rule)}
.rail .sn{font-size:14px}
.rail .sc{color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}
.rail .sh{grid-column:1/-1;color:var(--faint);font-size:12.5px}
.rail li.bad .sh{color:var(--flag)}
.rail li a:hover .sn,.rail li a[aria-current] .sn{color:var(--mark)}
.rail li a[aria-current] .sn{font-weight:700}
.allsrc{display:inline-block;font-size:13px;color:var(--muted);margin-bottom:8px}

.viewing{color:var(--muted);font-size:14px;margin-bottom:22px}
.viewing b{color:var(--ink)}
.entry{max-width:66ch;padding:0 0 30px;margin-bottom:30px;border-bottom:1px solid var(--rule)}
.entry h2{font:500 23px/1.35 var(--serif)}
.tl{text-decoration:none}
.tl mark{color:var(--ink);background:none;box-decoration-break:clone;-webkit-box-decoration-break:clone}
.entry.build .tl mark{background:linear-gradient(transparent 55%,var(--mark-bg) 55%)}
.entry.watch .tl mark{text-decoration:underline 2px var(--rival);text-underline-offset:5px}
.tl:hover mark{color:#fff}
.score{font-size:14.5px;color:var(--muted);margin-top:8px;display:flex;align-items:center;gap:4px 10px;flex-wrap:wrap}
.vw{font-weight:700}
.entry.build .vw{color:var(--mark)} .entry.watch .vw{color:var(--rival)}
.ticks{display:inline-flex;gap:3px}
.ticks i{width:7px;height:12px;border-radius:1px;background:var(--rule)}
.entry.build .ticks i.on{background:var(--mark)}
.entry.watch .ticks i.on{background:var(--rival)}
.what{font:400 18px/1.5 var(--serif);margin-top:10px}
.ns{font-size:15px;color:var(--ink);margin-top:10px;max-width:62ch}
.pitchbox{font-size:14.5px;margin-top:12px;background:var(--surface);border-radius:6px;padding:12px 14px;
  white-space:pre-wrap;max-width:62ch}
.pitchbox .report{color:var(--mark);margin-right:14px;font-size:14px}
.meta{color:var(--faint);font-size:13.5px;margin-top:10px}
.acts{display:flex;flex-wrap:wrap;gap:4px 18px;margin-top:10px}
.acts a,.acts button{font:14px/1.6 var(--sans);color:var(--muted);background:none;border:0;padding:0;cursor:pointer;
  text-decoration:underline;text-decoration-color:var(--faint);text-underline-offset:3px}
.acts a:hover,.acts button:hover{color:var(--ink);text-decoration-color:var(--ink)}

.row{max-width:66ch;padding:12px 0;border-bottom:1px solid var(--rule)}
.row h3{font:400 17px/1.4 var(--serif)}
.row .meta{margin-top:3px}
.row .acts{display:none}
.row:hover .acts,.row:focus-within .acts{display:flex}
.row.skip h3{color:var(--muted)}
.why{color:var(--muted);margin-right:8px}
.why::after{content:"."}
.why.flag{color:var(--flag)}

.pager{display:flex;gap:22px;align-items:center;margin:34px 0 0;font-size:14px;color:var(--muted)}
.pager a{color:var(--ink)}
.empty{color:var(--muted);padding:30px 0}
.empty p+p{margin-top:8px}

@media (max-width:860px){
  body{padding:24px 16px 80px}
  h1{font-size:34px}
  .lede{font-size:19px}
  .layout{grid-template-columns:1fr;gap:18px;margin-top:20px}
  .rail details:not([open]) summary{margin-bottom:0}
  .rail summary{text-decoration:underline dotted var(--faint);text-underline-offset:4px}
  .rail details:not([open]) summary::after{content:", show";font-weight:400}
  .entry h2{font-size:20px}
  .row .acts{display:flex}
}
</style></head>
<body>
{{HEADER}}
<p class="day">{{DAY}}</p>
<p class="lede">{{LEDE}}</p>
<div class="bar">
  <button type="button" onclick="fetchNow()">Fetch new ideas</button>
  <button type="button" class="second" onclick="evalNow()">Evaluate 25 more</button>
  <span id="status" class="say" role="status"></span>
</div>
<nav class="tabs" aria-label="Filter by verdict">{{TABS}}</nav>
<div class="layout">
{{RAIL}}
<main>{{VIEW}}{{ENTRIES}}{{PAGER}}</main>
</div>
{{JOBBAR}}
<script>
if (matchMedia('(max-width:860px)').matches) { const d = document.getElementById('srcs'); if (d) d.open = false; }
function say(text, tone){
  const s = document.getElementById('status');
  s.textContent = text;
  s.style.color = tone === 'bad' ? 'var(--flag)' : (tone === 'good' ? 'var(--ink)' : 'var(--muted)');
}
async function fetchNow(){
  say('Fetching new ideas…');
  const r = await (await fetch('/fetch?async_=1')).json();
  JobBar.watch(r.job, async (j) => {
    if (j.state !== 'done') { say('Fetch failed: ' + (j.message || 'unknown error'), 'bad'); return; }
    say((j.result || 'Fetched') + '. Evaluating the newest 15…', 'good');
    await fetch('/evaluate?async_=1&limit=15');
    setTimeout(() => location.reload(), 1200);
  });
}
async function evalNow(){
  say('Evaluating 25 ideas…');
  const r = await (await fetch('/evaluate?async_=1&limit=25')).json();
  JobBar.watch(r.job, (j) => {
    const ok = j.state === 'done';
    say(ok ? 'Evaluated. Reloading…' : 'Evaluation failed: ' + (j.message || ''), ok ? 'good' : 'bad');
    if (ok) setTimeout(() => location.reload(), 1200);
  });
}
async function explainIdea(id){
  const box = document.getElementById('ns_' + id);
  if (box.textContent) { box.hidden = !box.hidden; return; }
  box.hidden = false;
  box.textContent = 'Writing a plain explanation…';
  const r = await (await fetch('/explain/' + id)).json();
  box.textContent = r.nutshell;
}
async function deepeval(id){
  say('Running a deep evaluation…');
  const r = await (await fetch('/deepeval/' + id + '?async_=1')).json();
  JobBar.watch(r.job, (j) => {
    const ok = j.state === 'done';
    say(ok ? 'Report saved. Reloading…' : 'Deep evaluation failed: ' + (j.message || ''), ok ? 'good' : 'bad');
    if (ok) setTimeout(() => location.reload(), 1500);
  });
}
async function simulate(id){
  say('Simulating buyer reactions, about 6 to 10 minutes…');
  const r = await (await fetch('/simulate/' + id + '?async_=1')).json();
  if (r.status === 'existing') { say('Opening the saved simulation…', 'good'); location.href = '/simulation/' + id; return; }
  if (!r.job) { say('Simulation could not start: ' + (r.error || 'unknown error'), 'bad'); return; }
  JobBar.watch(r.job, (j) => {
    if (j.state === 'done') { say('Simulation saved. Opening…', 'good'); location.href = '/simulation/' + id; }
    else if (j.state === 'error') { say('Simulation failed: ' + (j.message || ''), 'bad'); }
    else { say(j.label || 'Simulating…'); }
  });
}
const DEMO_LINK = {{DEMO_LINK}};
function filledPitch(p){
  return p.replaceAll('{link}', DEMO_LINK).replaceAll('{name}', '').replace(/^\\s*Hi\\s*,/, 'Hi there,');
}
async function pitchIdea(id){
  const box = document.getElementById('pitch_' + id);
  if (box.dataset.done) { box.hidden = !box.hidden; return; }
  box.hidden = false;
  box.textContent = 'Drafting a pitch, about 10 seconds…';
  const r = await (await fetch('/pitch/' + id)).json();
  if (!r.pitch) { box.textContent = 'Pitch failed: ' + r.status; return; }
  const msg = filledPitch(r.pitch);
  box.textContent = msg;
  const bar = document.createElement('div');
  bar.style.marginTop = '10px';
  const cp = document.createElement('a'); cp.href = '#'; cp.className = 'report'; cp.textContent = 'Copy';
  cp.onclick = (e) => { e.preventDefault(); navigator.clipboard.writeText(msg); cp.textContent = 'Copied';
    setTimeout(() => cp.textContent = 'Copy', 1200); };
  const wa = document.createElement('a'); wa.className = 'report'; wa.target = '_blank'; wa.textContent = 'Open in WhatsApp';
  wa.href = 'https://wa.me/?text=' + encodeURIComponent(msg);
  const out = document.createElement('a'); out.className = 'report'; out.textContent = 'Add to Outreach';
  out.href = '/outreach?prefill=' + id;
  bar.append(cp, wa, out);
  box.appendChild(bar);
  box.dataset.done = 1;
}
</script>
</body></html>"""


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        total, report = fetch_all()
        print(f"[{datetime.now()}] +{total} items | " + " | ".join(report))
    elif len(sys.argv) > 1 and sys.argv[1] == "evaluate":
        n, used = evaluate_items(int(sys.argv[2]) if len(sys.argv) > 2 else 25)
        print(f"[{datetime.now()}] evaluated {n} items | models: {json.dumps(used)}")
    elif len(sys.argv) > 1 and sys.argv[1] == "extract":
        # stage 2: pain post -> structured pain with a verified quote (pain_extract.py)
        import pain_extract
        conn = db()
        nums = [a for a in sys.argv[2:] if a.isdigit()]
        s = pain_extract.run(
            conn, limit=int(nums[0]) if nums else 40, redo="--redo" in sys.argv,
            progress=lambda i, t, p: print("  %-10s %d  %s" % (p["status"], i, (t or "")[:70]),
                                           flush=True))
        left = len(pain_extract.pending(conn))
        conn.close()
        print("[%s] extract: done=%d pain=%d unverified=%d builder=%d not_pain=%d error=%d"
              " | pending=%d" % (datetime.now(), s["done"], s["PAIN"], s["UNVERIFIED"],
                                 s["BUILDER"], s["NOT_PAIN"], s["ERROR"], left))
    elif len(sys.argv) > 1 and sys.argv[1] == "simulate":
        dry = "--dry-run" in sys.argv
        sim_limit = 3
        for a in sys.argv[2:]:
            if a.isdigit():
                sim_limit = int(a)
        if dry:
            rows = pending_simulations(limit=sim_limit)
            print("[%s] dry run: %d idea(s) would be simulated" % (datetime.now(), len(rows)))
            for r in rows:
                print("  %d  %s  |  %s" % (r["id"], r["source"], r["title"][:70]))
        else:
            s = run_simulations(limit=sim_limit, max_seconds=1800)
            print("[%s] simulate: attempted=%d done=%d failed=%d skipped=%d %s"
                  % (datetime.now(), s["attempted"], s["done"], s["failed"],
                     s["skipped"], s["error"]))
            if s["error"]:
                sys.exit(1)
    else:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=8300)
