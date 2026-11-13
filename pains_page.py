"""/pains — the output of stage 2 (pain_extract.py), readable at a glance.

Each entry is one Reddit post the model read, led by the one quote that was
checked word for word against the post, then who has the problem, how they cope
today, what it costs them and any sign they would pay.

Tabs split the statuses: PAIN (leads), BUILDER (rivals working on the same pain),
and the rest. The tool index on the left is the cheapest early hint of a cluster
before stage 3 exists: the same tool named by many different authors. Clicking a
tool filters the list to posts that name it.

Design: a casebook, not a card grid. The verified quote carries a highlighter
mark (yellow for a pain, blue underline for a rival), so status is read from the
evidence itself rather than from a badge.
"""
import html
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import quote as urlquote

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import theme

BASE = Path(__file__).parent
DB = BASE / "ideas.db"

router = APIRouter()

TABS = [  # (query value, status in the table, label)
    ("pain", "PAIN", "Pains"),
    ("builder", "BUILDER", "Builders"),
    ("not_pain", "NOT_PAIN", "Not a pain"),
    ("unverified", "UNVERIFIED", "Unverified"),
    ("error", "ERROR", "Errors"),
    ("all", None, "All"),
]
KIND = {"PAIN": "pain", "BUILDER": "builder", "NOT_PAIN": "other",
        "UNVERIFIED": "flag", "ERROR": "flag"}
STATUS_WORDS = {"PAIN": "Pain", "BUILDER": "Builder", "NOT_PAIN": "Not a pain",
                "UNVERIFIED": "Quote not found in post", "ERROR": "Model call failed"}


def esc(s):
    return html.escape(str(s or ""), quote=True)


def _load(status):
    """(rows for this tab, counts per status). Empty if extraction never ran."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"items", "pains"} <= have:
            return [], {}
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM pains GROUP BY status").fetchall())
        where, params = "", []
        if status:
            where, params = "WHERE p.status = ?", [status]
        rows = conn.execute(
            "SELECT i.id, i.title, i.url, i.meta, i.author, p.* FROM pains p"
            " JOIN items i ON i.id = p.item_id " + where + " ORDER BY i.id DESC", params).fetchall()
        return rows, counts
    finally:
        conn.close()


def _tools(row):
    try:
        return [t.strip() for t in json.loads(row["tools"] or "[]") if isinstance(t, str) and t.strip()]
    except ValueError:
        return []


def _tally(rows):
    """[(tool name, number of posts naming it)], case-insensitive, most named first."""
    seen, spelling = Counter(), {}
    for r in rows:
        for key, name in {t.lower(): t for t in _tools(r)}.items():
            spelling.setdefault(key, name)
            seen[key] += 1
    return [(spelling[k], n) for k, n in seen.most_common(18)]


def _source(r):
    """'r/realtors, 7 comments, by u/someone' from the stored url, meta and author."""
    sub = re.search(r"reddit\.com/(r/[^/]+)/", r["url"] or "")
    comments = re.search(r"(\d+) comments?", r["meta"] or "")
    parts = [sub.group(1) if sub else ""]
    if comments:
        parts.append(f'{comments.group(1)} comment{"" if comments.group(1) == "1" else "s"}')
    if r["author"]:
        parts.append(f'by u/{r["author"]}')
    return ", ".join(p for p in parts if p)


def _fact(label, value):
    return f"<dt>{label}</dt><dd>{esc(value)}</dd>" if value else ""


def _entry(r, show_status):
    st = r["status"] or ""
    kind = KIND.get(st, "other")
    if r["quote"]:
        lead = f'<blockquote class="q"><mark>{esc(r["quote"])}</mark></blockquote>'
    else:
        lead = f'<p class="q q-none">{esc(r["job"] or r["title"])}</p>'
    status = f'<span class="st">{STATUS_WORDS.get(st, esc(st))}. </span>' if show_status else ""
    facts = (_fact("Wants to", r["job"] if r["quote"] else "") + _fact("Today", r["workaround"])
             + _fact("Costs them", r["cost_signal"]) + _fact("Would pay", r["pay_signal"])
             + (_fact("Error", r["error"]) if st == "ERROR" else ""))
    tools = "".join(f'<a class="tool" href="/pains?status=all&amp;tool={esc(urlquote(t.lower()))}">{esc(t)}</a>'
                    for t in _tools(r))
    return (
        f'<article class="entry {kind}">'
        f'{lead}'
        f'<p class="who">{status}{esc(r["persona"])}</p>'
        + (f'<dl>{facts}</dl>' if facts else "")
        + f'<p class="src"><a class="post" href="{esc(r["url"])}" target="_blank" rel="noopener">{esc(r["title"])}</a>'
        f'<span class="where">{esc(_source(r))}</span></p>'
        + (f'<p class="tools">{tools}</p>' if tools else "")
        + '</article>')


def _headline(counts):
    pains = counts.get("PAIN", 0)
    builders = counts.get("BUILDER", 0)
    total = sum(counts.values())
    if not total:
        return "No posts have been read yet."
    people = "person describes" if pains == 1 else "people describe"
    if builders == 0:
        rivals = "Nobody here is building a fix yet."
    else:
        rivals = ("One is" if builders == 1 else f"{builders} are") + " already building a fix."
    return f"{pains} {people} a problem in their own work. {rivals}"


@router.get("/pains", response_class=HTMLResponse)
def pains(status: str = "pain", tool: str = ""):
    tab = next((t for t in TABS if t[0] == status), TABS[0])
    rows, counts = _load(tab[1])
    tally = _tally(rows)
    tool = tool.strip().lower()
    if tool:
        rows = [r for r in rows if tool in {t.lower() for t in _tools(r)}]

    def count(t):
        return sum(counts.values()) if t[1] is None else counts.get(t[1], 0)
    tabs = "".join(
        f'<a class="tab" href="/pains?status={t[0]}"{" aria-current=page" if t is tab and not tool else ""}>'
        f'{t[2]} ({count(t)})</a>'
        for t in TABS if t[0] in ("pain", "builder", "all") or count(t))

    top = tally[0][1] if tally else 1
    index = "".join(
        f'<li><a href="/pains?status={tab[0]}&amp;tool={esc(urlquote(n.lower()))}"'
        f'{" aria-current=true" if n.lower() == tool else ""}>'
        f'<span class="tn">{esc(n)}</span><span class="tc">{c}</span>'
        f'<span class="meter" style="width:{max(8, round(100 * c / top))}%"></span></a></li>'
        for n, c in tally)
    index_html = (f'<aside class="index"><h2>Named in these posts</h2><ul>{index}</ul>'
                  + (f'<a class="clear" href="/pains?status={tab[0]}">Show all tools</a>' if tool else "")
                  + '</aside>') if tally else ""

    filtered = f'<p class="filter">Posts naming <b>{esc(tool)}</b></p>' if tool else ""
    entries = "".join(_entry(r, tab[1] is None or bool(tool)) for r in rows) or (
        '<div class="empty"><p>Nothing to read here yet.</p>'
        '<p>Run <code>python app.py extract 60</code> after a fetch, or wait for the 9:00 run.</p></div>')

    body = (theme.header("pains", "Pains")
            + f'<p class="lede solo">{esc(_headline(counts))}</p>'
            + '<p class="how">Read from Reddit by a free model. Every highlighted quote was found word for word'
              ' in the post, and tools are listed only when the post names them. Builders are not leads,'
              ' but they prove the pain is real.</p>'
            + f'<nav class="tabs" aria-label="Filter by status">{tabs}</nav>'
            + f'<div class="layout{"" if index_html else " solo"}">{index_html}'
            + f'<main>{filtered}{entries}</main></div>')
    return HTMLResponse(theme.page("Pains", body, CSS, tab_title="Pains — IdeaPulse"))


CSS = """
.layout{display:grid;grid-template-columns:220px minmax(0,1fr);gap:56px;margin-top:34px}
.layout.solo{grid-template-columns:minmax(0,1fr)}
.index h2{font:700 13px/1.3 var(--sans);color:var(--muted);margin-bottom:12px}
.index ul{list-style:none}
.index li a{display:grid;grid-template-columns:1fr auto;row-gap:4px;text-decoration:none;
  padding:6px 0;font-size:14px}
.index .tc{color:var(--muted);font-variant-numeric:tabular-nums}
.index .meter{grid-column:1/-1;height:3px;background:var(--rule);border-radius:2px}
.index li a:hover .tn,.index li a[aria-current] .tn{color:var(--mark)}
.index li a[aria-current] .meter{background:var(--mark)}
.index .clear{display:inline-block;margin-top:12px;font-size:13px;color:var(--muted)}

.filter{color:var(--muted);font-size:14px;margin-bottom:22px}
.filter b{color:var(--ink)}
.entry{max-width:64ch;padding:0 0 30px;margin-bottom:30px;border-bottom:1px solid var(--rule)}
.entry:last-child{border-bottom:0}
.q{font:400 22px/1.5 var(--serif);margin-bottom:12px}
.q mark{color:var(--ink);background:none;padding:0 .1em;box-decoration-break:clone;-webkit-box-decoration-break:clone}
.entry.pain .q mark{background:linear-gradient(transparent 52%,var(--mark-bg) 52%)}
.entry.builder .q mark{text-decoration:underline 2px var(--rival);text-underline-offset:5px}
.entry.other .q,.entry.flag .q{color:var(--muted)}
.entry.other .q mark,.entry.flag .q mark{color:var(--muted)}
.q-none{font-style:italic}
.who{font-size:15px;color:var(--ink)}
.st{font-weight:700}
.entry.builder .st{color:var(--rival)} .entry.flag .st{color:var(--flag)}
.entry.pain .st{color:var(--mark)} .entry.other .st{color:var(--muted)}
dl{display:grid;grid-template-columns:auto 1fr;gap:4px 16px;margin:12px 0 0;font-size:14.5px}
dt{color:var(--muted)}
dd{color:var(--ink)}
.src{margin-top:14px;font-size:14px;color:var(--muted)}
.post{font:italic 400 16px/1.4 var(--serif);color:var(--ink);text-decoration:underline;
  text-decoration-color:var(--faint);text-underline-offset:3px;margin-right:10px}
.post:hover{text-decoration-color:var(--ink)}
.where{white-space:nowrap}
.tools{margin-top:10px;display:flex;gap:6px 14px;flex-wrap:wrap}
.tool{font-size:13px;color:var(--muted);text-decoration:none;border-bottom:1px dotted var(--faint)}
.tool:hover{color:var(--ink)}

@media (max-width:860px){
  .layout{grid-template-columns:1fr;gap:24px;margin-top:22px}
  .index ul{display:flex;flex-wrap:wrap;gap:4px 16px}
  .index li a{display:inline-flex;gap:6px;padding:2px 0}
  .index .meter{display:none}
  .q{font-size:19px}
  .where{white-space:normal}
}
"""
