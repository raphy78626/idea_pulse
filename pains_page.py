"""/pains — the output of stage 2 (pain_extract.py), readable at a glance.

Each card is one Reddit post the model read: who has the problem, the job they
are trying to do, how they cope today, what it costs them, any sign they would
pay, and one quote that was checked word for word against the post.

Tabs split the statuses: PAIN (leads), BUILDER (rivals working on the same pain),
and the rest. The tool tally at the top is the cheapest early hint of a cluster
before stage 3 exists: the same tool named by many different authors.
"""
import html
import json
import sqlite3
from collections import Counter
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

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
BADGE = {"PAIN": "pain", "BUILDER": "builder", "NOT_PAIN": "no",
         "UNVERIFIED": "unv", "ERROR": "err"}


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
        return [t for t in json.loads(row["tools"] or "[]") if isinstance(t, str)]
    except ValueError:
        return []


def _tally(rows):
    """Tool name -> number of distinct authors naming it, case-insensitive."""
    seen, spelling = Counter(), {}
    for r in rows:
        for key, name in {t.strip().lower(): t.strip() for t in _tools(r)}.items():
            spelling.setdefault(key, name)
            seen[key] += 1
    return [(spelling[k], n) for k, n in seen.most_common(15)]


def _line(label, value):
    return f'<div class="ln"><span>{label}</span> {esc(value)}</div>' if value else ""


def _card(r):
    st = r["status"] or ""
    tools = "".join(f'<span class="tool">{esc(t)}</span>' for t in _tools(r))
    quote = f'<blockquote>“{esc(r["quote"])}”</blockquote>' if r["quote"] else ""
    err = _line("Error", r["error"]) if st == "ERROR" else ""
    who = f' · u/{esc(r["author"])}' if r["author"] else ""
    return (
        f'<div class="card {BADGE.get(st, "no")}">'
        f'<div class="head"><span class="vb {BADGE.get(st, "no")}">{esc(st)}</span>'
        f'<span class="who">{esc(r["persona"])}</span></div>'
        f'<div class="title"><a href="{esc(r["url"])}" target="_blank" rel="noopener">{esc(r["title"])}</a></div>'
        f'{quote}'
        f'{_line("Job", r["job"])}{_line("Today", r["workaround"])}'
        f'{_line("Cost", r["cost_signal"])}{_line("Would pay", r["pay_signal"])}{err}'
        f'<div class="meta">{tools}<span class="src">{esc(r["meta"])}{who} · read {esc(r["extracted_on"])}</span></div>'
        f'</div>')


@router.get("/pains", response_class=HTMLResponse)
def pains(status: str = "pain"):
    tab = next((t for t in TABS if t[0] == status), TABS[0])
    rows, counts = _load(tab[1])

    def count(t):
        return sum(counts.values()) if t[1] is None else counts.get(t[1], 0)
    chips = "".join(
        f'<a class="chip {"on" if t is tab else ""}" href="/pains?status={t[0]}">{t[2]} ({count(t)})</a>'
        for t in TABS if t[0] in ("pain", "builder", "all") or count(t))

    tally = _tally(rows)
    tally_html = ('<div class="tally"><b>Tools named</b> '
                  + "".join(f'<span class="tool">{esc(n)} ×{c}</span>' for n, c in tally)
                  + '</div>') if tally else ""

    cards = "".join(_card(r) for r in rows) or (
        '<div class="empty">No extracted pains here yet.<br>'
        'Run <code>python app.py extract 60</code> after a fetch, or wait for the 9:00 cron.</div>')

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pains — IdeaPulse</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0b141a;color:#e9edef;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
  padding:26px;max-width:920px;margin:0 auto;line-height:1.5}}
h1{{font-size:25px;font-weight:800;display:inline}}
.navout{{color:#4ade80;font-size:14px;text-decoration:none;margin-left:12px}}
.sub{{color:#8fa3ad;font-size:13.5px;margin:10px 0 14px;max-width:780px}}
.sub b{{color:#e9edef}}
.chips{{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 10px}}
.chip{{background:#111b21;border:1px solid #1d2a33;color:#8fa3ad;border-radius:16px;
  padding:6px 13px;font-size:12.5px;text-decoration:none;font-weight:500}}
.chip:hover{{border-color:#00a884;color:#e9edef}}
.chip.on{{background:#008069;color:#fff;border-color:#008069}}
.tally{{color:#8fa3ad;font-size:12px;margin:4px 0 14px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}}
.tally b{{color:#e9edef;margin-right:4px}}
.card{{background:#111b21;border:1px solid #1d2a33;border-radius:12px;padding:13px 16px;margin-bottom:9px}}
.card.pain{{border-left:4px solid #4ade80}}
.card.builder{{border-left:4px solid #fbbf24}}
.card.no{{opacity:.55;border-left:4px solid #334155}}
.card.unv,.card.err{{border-left:4px solid #f87171}}
.head{{display:flex;align-items:center;gap:9px;margin-bottom:4px}}
.vb{{font-size:10.5px;font-weight:800;padding:2px 9px;border-radius:9px;letter-spacing:.4px}}
.vb.pain{{background:#052e1b;color:#4ade80;border:1px solid #14532d}}
.vb.builder{{background:#2e2605;color:#fbbf24;border:1px solid #713f12}}
.vb.no{{background:#1d2a33;color:#94a3b8}}
.vb.unv,.vb.err{{background:#2a0f0f;color:#f87171}}
.who{{color:#93c5fd;font-size:12.5px}}
.title{{font-size:15px;font-weight:500;line-height:1.4}}
.title a{{color:inherit;text-decoration:none}}
.title a:hover{{color:#4ade80}}
blockquote{{color:#d1fae5;font-size:13.5px;margin:8px 0 6px;padding:6px 12px;
  border-left:3px solid #14532d;background:#0a1f16;border-radius:0 8px 8px 0}}
.ln{{font-size:12.8px;color:#cbd5e1;margin-top:3px}}
.ln span{{color:#667781;display:inline-block;min-width:74px;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}}
.meta{{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}}
.tool{{font-size:11px;padding:2px 9px;border-radius:9px;background:#1d2a33;color:#93c5fd;font-weight:600}}
.src{{color:#4a6270;font-size:11.5px}}
.empty{{color:#8fa3ad;text-align:center;padding:44px 20px;font-size:13.5px;line-height:1.8}}
code{{background:#1d2a33;padding:1px 6px;border-radius:5px;color:#e9edef}}
@media(max-width:640px){{
 body{{padding:18px 16px 60px}}
 h1{{font-size:21px}}
 .ln span{{display:block;min-width:0}}
}}
</style></head><body>
<h1>🩹 Pains</h1>
<a class="navout" href="/">← IdeaPulse</a>
<a class="navout" href="/outreach">📤 Outreach</a>
<div class="sub">Reddit posts where someone describes a problem in their own work, read by a free model.
Every quote below was <b>checked word for word against the post</b>; tools are only listed if the post names them.
<b>Builders</b> are people already working on a fix: not leads, but proof the pain is real and a rival to count.</div>
<div class="chips">{chips}</div>
{tally_html}
{cards}
</body></html>""")
