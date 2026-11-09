"""Custom source example: HN front page (best of last 48h).

Drop any .py file in sources/ with a fetch(client) function returning
[(title, url, meta, kind), ...] — IdeaPulse auto-discovers it.
"""


def fetch(client):
    r = client.get("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=20")
    items = []
    for h in r.json().get("hits", []):
        title = h.get("title") or ""
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        items.append((title, url, f"front page · {h.get('points', 0)} pts", "show_hn"))
    return items
