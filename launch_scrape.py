"""Scrapers for launch directories that actually expose product data.

Each adapter returns [(title, url, meta, kind)] — the same shape `sources/*.py`
uses — so these drop straight into IdeaPulse's existing fetch → evaluate pipeline.

Verified 2026-11-08 against live sites. Sites that only render products client-side
or block bots (Toolify 403, Future Tools 403, Indie Hackers Cloudflare, PitchWall /
TryLaunch / ProductBurst JS-only) are deliberately absent rather than shipped broken.

Why the adapters differ so much:
  - Product Hunt serves an Atom feed whose <entry> tags the old scraper missed
    (it looked for <item>, so it silently returned 0 every run).
  - Fazier embeds a Next.js __NEXT_DATA__ blob with real taglines and pricing.
  - BuildHop publishes full schema.org Product nodes with the *external* product
    URL — the only source that hands us a real destination, not a listing page.
  - Awesome Indie / OpenHunts publish ItemList of names + listing URLs, so we
    fetch each detail page for its og:description (~1.3 s for 12, 6 threads).
"""
import html
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _get(client, url, timeout=20):
    """Use the shared httpx client when the pipeline provides one, else urllib."""
    if client is not None:
        r = client.get(url, timeout=timeout)
        return r.text
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def _clean(s, limit=200):
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _ld_json(html_text):
    """Every parsed application/ld+json block (skips broken ones)."""
    out = []
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                         html_text, re.S):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return out


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _og_description(client, url):
    """og:description from a listing page — the only cheap way to get real copy."""
    try:
        t = _get(client, url, timeout=12)
    except Exception:
        return ""
    m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)', t)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:description["\']', t)
    return _clean(m.group(1), 220) if m else ""


def _enrich(client, rows, workers=6):
    """Fill in missing descriptions from listing pages, concurrently."""
    todo = [(i, r) for i, r in enumerate(rows) if not r[2] or r[2] == "launched"]
    if not todo:
        return rows
    def work(pair):
        i, r = pair
        return i, _og_description(client, r[1])
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, desc in ex.map(work, todo):
            if desc:
                title, url, meta, kind = rows[i]
                rows[i] = (title, url, desc, kind)
    return rows


# ---------------------------------------------------------------- adapters

def producthunt(client):
    """Atom feed — the app's built-in fetch_ph parses <item> and always gets 0."""
    t = _get(client, "https://www.producthunt.com/feed", timeout=20)
    rows = []
    for block in re.findall(r"<entry>(.*?)</entry>", t, re.S):
        tm = re.search(r"<title[^>]*>(.*?)</title>", block, re.S)
        cm = re.search(r"<content[^>]*>(.*?)</content>", block, re.S)
        lm = re.search(r'<link[^>]+href="([^"]+)"', block)
        if not tm:
            continue
        name = _clean(tm.group(1), 120)
        if not name:
            continue
        body = _clean(cm.group(1), 220) if cm else ""
        # PH puts the tagline in the first <p> of the content
        tagline = ""
        if cm:
            first_p = re.search(r"<p>(.*?)</p>", html.unescape(cm.group(1)), re.S)
            if first_p:
                tagline = _clean(first_p.group(1), 200)
        url = (lm.group(1) if lm else "https://www.producthunt.com").split("?")[0]
        meta = tagline or body or "Product Hunt launch"
        rows.append((name, url, meta, "launch"))
    return rows[:30]


def fazier(client):
    """Next.js payload with taglines, pricing type and upvotes."""
    t = _get(client, "https://fazier.com/", timeout=20)
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', t, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    posts, seen = [], set()
    for node in _walk(data):
        name, slug = node.get("name"), node.get("slug")
        if not (isinstance(name, str) and isinstance(slug, str)):
            continue
        if "upvotes_count" not in node and "tagline" not in node:
            continue
        if slug in seen:
            continue
        seen.add(slug)
        tag = _clean(node.get("tagline"), 160)
        price = _clean(node.get("pricing_type"), 20)
        votes = node.get("upvotes_count") or 0
        meta = " · ".join(x for x in (tag, price, f"{votes} upvotes" if votes else "") if x)
        posts.append((_clean(name, 120), f"https://fazier.com/launches/{slug}", meta, "launch"))
    return posts[:30]


def buildhop(client):
    """schema.org Product nodes — the external product URL lives in item.url."""
    t = _get(client, "https://buildhop.io/", timeout=20)
    rows = []
    for block in _ld_json(t):
        for node in _walk(block):
            if node.get("@type") != "ItemList" or "Leaderboard" not in str(node.get("name", "")):
                continue
            for el in node.get("itemListElement") or []:
                p = el.get("item") if isinstance(el, dict) else None
                if not isinstance(p, dict):
                    continue
                name = _clean(p.get("name"), 120)
                url = p.get("url") or el.get("url")
                if not (name and url):
                    continue
                cat = _clean(p.get("category"), 40)
                desc = _clean(p.get("description"), 200)
                meta = " · ".join(x for x in (cat, desc) if x)
                rows.append((name, str(url), meta or "BuildHop launch", "launch"))
    return rows


def awesomeindie(client):
    """ItemList of names + listing URLs → detail pages for descriptions."""
    t = _get(client, "https://awesomeindie.com/", timeout=20)
    rows, seen = [], set()
    for block in _ld_json(t):
        for node in _walk(block):
            if "ItemList" not in (node.get("@type") if isinstance(node.get("@type"), list)
                                  else [node.get("@type")]):
                continue
            for el in node.get("itemListElement") or []:
                if not isinstance(el, dict):
                    continue
                name, url = _clean(el.get("name"), 120), el.get("url")
                if name and url and url not in seen:
                    seen.add(url)
                    rows.append((name, str(url), "launched", "launch"))
    return _enrich(client, rows[:24])


def openhunts(client):
    t = _get(client, "https://openhunts.com/", timeout=20)
    rows, seen = [], set()
    for block in _ld_json(t):
        for node in _walk(block):
            types = node.get("@type") if isinstance(node.get("@type"), list) else [node.get("@type")]
            if "ItemList" not in types:
                continue
            for el in node.get("itemListElement") or []:
                if not isinstance(el, dict):
                    continue
                name, url = _clean(el.get("name"), 120), el.get("url")
                if name and url and url not in seen:
                    seen.add(url)
                    rows.append((name, str(url), "launched", "launch"))
    return _enrich(client, rows[:24])


def uneed(client):
    """Nuxt app; the product names live only on the detail pages."""
    t = _get(client, "https://uneed.best/", timeout=20)
    slugs = []
    for href in re.findall(r'href="(/tool/[a-z0-9\-]{2,60})"', t):
        if href not in slugs:
            slugs.append(href)
    rows = []
    for href in slugs[:20]:
        url = "https://www.uneed.best" + href
        name = href.split("/tool/")[-1].replace("-", " ").title()
        rows.append((name, url, "launched", "launch"))
    return _enrich(client, rows)


ADAPTERS = {
    "Fazier": fazier,
    "BuildHop": buildhop,
    "Awesome Indie": awesomeindie,
    "OpenHunts": openhunts,
    "Uneed": uneed,
}


def fetch(client=None):
    """Run every adapter; a failing site never kills the run.
    Returns the pipeline's (title, url, meta, kind) list."""
    out, report = [], {}
    for name, fn in ADAPTERS.items():
        try:
            got = fn(client)
            report[name] = len(got)
            out.extend(got)
        except Exception as e:
            report[name] = f"FAILED {type(e).__name__}"
    if report:
        import sys
        print(f"[launch_scrape] {report}", file=sys.stderr)
    return out


def fetch_report(client=None):
    """Same as fetch() but also returns the per-site counts (for debugging)."""
    out, report = [], {}
    for name, fn in ADAPTERS.items():
        try:
            got = fn(client)
            report[name] = len(got)
            out.extend(got)
        except Exception as e:
            report[name] = f"FAILED {type(e).__name__}"
    return out, report


if __name__ == "__main__":
    items, rep = fetch_report()
    print(json.dumps(rep, indent=1))
    print(f"\n{len(items)} items")
    for it in items[:6]:
        print(f"  {it[0][:38]:<40} {it[2][:52]:<54} {it[1][:48]}")
