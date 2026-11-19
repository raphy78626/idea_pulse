"""First-currency board — the answer to "what can I sell this week?".

Every other page in IdeaPulse ranks *products*. This one ranks **offers**: what
you could sell, to whom, at what price, and how many days until a stranger pays.
It is deliberately biased toward boring, sellable services over clever products,
because the goal is a first payment, not a launch.

Data comes from business_scrape.py (Reddit business subs + HN demand phrases) and
is scored by MONEY_SYSTEM in app.py.
"""
import html
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import theme

BASE = Path(__file__).parent
DB = BASE / "ideas.db"
# sources that describe opportunities rather than already-built products
OPPORTUNITY_SOURCES = ("Reddit business ideas", "HN demand signals")

# launch-directory products that scored BUILD — possible products to sell
PRODUCT_SOURCES = ("Product Hunt", "Fazier", "BuildHop", "Awesome Indie",
                   "OpenHunts", "Uneed")

router = APIRouter()

def esc(s):
    return html.escape(str(s or ""), quote=True)


def _conn():
    import pain_extract
    conn = sqlite3.connect(DB)
    pain_extract.ensure_table(conn)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(lane, verdict, order):
    conn = _conn()
    marks = ",".join("?" * len(OPPORTUNITY_SOURCES))
    where, params = [], []

    if lane == "pains":
        where.append("p.status = 'PAIN'")
    elif lane == "products":
        m2 = ",".join("?" * len(PRODUCT_SOURCES))
        where.append(f"i.source IN ({m2})")
        params.extend(PRODUCT_SOURCES)
        where.append("i.verdict IN ('BUILD','WATCH')")
    else:
        # default ("opportunities"): business/demand lanes plus verified pains,
        # strictly excluding builders/not-pain rows from `pains`.
        where.append(f"(i.source IN ({marks}) OR p.status = 'PAIN')")
        where.append("(p.status IS NULL OR p.status = 'PAIN')")
        params.extend(OPPORTUNITY_SOURCES)

    if verdict == "go":
        where.append("i.earn_verdict='GO'")
    elif verdict == "later":
        where.append("i.earn_verdict='LATER'")
    elif verdict == "pending":
        where.append("(i.earn_verdict IS NULL OR i.earn_verdict='ERROR')")

    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    ordsql = {
        "quickest": "ORDER BY CASE WHEN i.first_payment>0 THEN 0 ELSE 1 END, i.first_payment, i.capital",
        "cheapest": "ORDER BY i.capital, i.first_payment",
        "hottest": "ORDER BY i.eng DESC, i.score DESC",
    }.get(order, "ORDER BY CASE i.earn_verdict WHEN 'GO' THEN 0 WHEN 'LATER' THEN 1 "
                   "WHEN 'NO' THEN 3 ELSE 2 END, i.first_payment, i.eng DESC")
    rows = conn.execute(
        f"""SELECT i.*, p.status AS pain_status, p.persona AS pain_persona,
                   p.job AS pain_job, p.quote AS pain_quote
            FROM items i LEFT JOIN pains p ON p.item_id = i.id
            {wsql} {ordsql} LIMIT 120""",
        params).fetchall()
    conn.close()
    return rows


def _stats():
    conn = _conn()
    marks = ",".join("?" * len(OPPORTUNITY_SOURCES))
    rows = conn.execute(
        f"""SELECT i.earn_verdict, COUNT(*) c
            FROM items i LEFT JOIN pains p ON p.item_id = i.id
            WHERE (i.source IN ({marks}) OR p.status = 'PAIN')
              AND (p.status IS NULL OR p.status = 'PAIN')
            GROUP BY i.earn_verdict""",
        OPPORTUNITY_SOURCES).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


PROOF_WORDS = {"yes": "Someone earns from this today", "partial": "Some evidence",
               "no": "No evidence yet"}
TIER = {"GO": "hi", "LATER": "lo", "NO": "off"}
VERDICT_WORDS = {"GO": "Sell now.", "LATER": "Later.", "NO": "Not a quick first payment.",
                 "ERROR": "Check failed, will retry."}


def _days(v):
    """first_payment is stored as TEXT ('7', '7 days', ''); return whole days or 0."""
    m = re.search(r"\d+", str(v or ""))
    return int(m.group()) if m else 0


def _card(r):
    def g(k):
        try:
            return r[k]
        except (IndexError, KeyError):
            return None

    v = (g("earn_verdict") or "").upper()
    days = _days(g("first_payment"))
    cap = g("capital")
    bits = []
    if days:
        bits.append(f"first payment in about {days} day{'' if days == 1 else 's'}")
    if v and v != "ERROR" and cap is not None:
        bits.append("nothing to spend to start" if not cap else f"about ${cap} to start")
    detail = (" " + ", ".join(bits) + ".") if bits else ""
    detail = detail[:2].upper() + detail[2:] if bits else ""
    verdict = f'<p class="verdict"><span class="vw">{VERDICT_WORDS.get(v, "Not checked yet.")}</span>{esc(detail)}</p>'

    facts = "".join(f"<dt>{label}</dt><dd>{esc(val)}</dd>" for label, val in (
        ("Sell", g("what")), ("To", g("buyer") or g("pain_persona")), ("Price", g("offer_money")),
        ("Why", g("reason")), ("Quote", g("pain_quote")),
        ("Evidence", PROOF_WORDS.get((g("proof") or "").lower(), ""))) if val)
    meta = (g("meta") or "").replace(" · ", ", ")
    meta = meta if len(meta) <= 140 else meta[:137].rstrip(" ,") + "…"
    where = ", ".join(x for x in (g("source"), meta) if x)
    item_id = g("id")
    action = (f'<p class="acts"><a class="lead-link" href="/outreach?prefill={item_id}">'
              f'Add to outreach</a></p>') if item_id else ""

    return (f'<article class="entry {TIER.get(v, "off")}">'
            f'<h2><a class="tl" href="{esc(g("url"))}" target="_blank" rel="noopener">'
            f'<mark>{esc((g("title") or "")[:160])}</mark></a></h2>'
            f'{verdict}'
            + (f'<dl class="facts">{facts}</dl>' if facts else "")
            + f'<p class="meta">{esc(where)}</p>{action}</article>')


@router.post("/money/scrape")
@router.get("/money/scrape")
def money_scrape(async_: int = 0):
    """Scrape only the business/hustle lanes, not the whole feed."""
    import app as ip
    import business_scrape
    import httpx
    import jobs

    def work(tick, label):
        total, report = 0, []
        srcs = list(business_scrape.ADAPTERS.items())
        with httpx.Client(headers=ip.UA, timeout=40, follow_redirects=True) as client:
            for i, (name, fn) in enumerate(srcs, 1):
                label(f"{i}/{len(srcs)} · {name}")
                try:
                    got = fn(client)
                    n = ip.save_items(name, got)
                    ip.set_status(name, "ok", n)
                    total += n
                    report.append(f"{name} +{n}")
                except Exception as e:
                    ip.set_status(name, f"FAILED {type(e).__name__}", 0)
                    report.append(f"{name} FAILED")
                tick(1)
        label(f"{total} opportunities found")
        return {"new_items": total, "sources": report}

    if async_:
        return {"job": jobs.start("Scraping business ideas", len(business_scrape.ADAPTERS), work)}
    return work(lambda n=1: None, lambda m: None)


@router.post("/money/evaluate")
@router.get("/money/evaluate")
def money_evaluate(limit: int = 12, async_: int = 0):
    """Score opportunities (including verified pains, never builders) for the first-currency question."""
    import app as ip
    import jobs
    import pain_extract

    limit = max(1, min(limit, 40))
    srcs = OPPORTUNITY_SOURCES + PRODUCT_SOURCES

    def work(tick, label):
        conn = ip.db()
        pain_extract.ensure_table(conn)
        conn.row_factory = sqlite3.Row
        marks = ",".join("?" * len(srcs))
        rows = conn.execute(
            f"""SELECT i.id, i.title, i.meta, i.source, i.body,
                       p.status AS pain_status, p.persona, p.job, p.workaround,
                       p.tools, p.cost_signal, p.pay_signal, p.quote
                FROM items i LEFT JOIN pains p ON p.item_id = i.id
                WHERE ((i.source IN ({marks}) AND (p.status IS NULL OR p.status='PAIN'))
                       OR p.status = 'PAIN')
                  AND (i.earn_verdict IS NULL OR i.earn_verdict='ERROR')
                ORDER BY CASE WHEN p.status='PAIN' THEN 0 ELSE 1 END,
                         i.verdict IS NULL, i.score DESC, i.eng DESC LIMIT ?""",
            (*srcs, limit)).fetchall()
        conn.close()
        used = {}
        for i, r in enumerate(rows, 1):
            label(f"{i}/{len(rows)} · {r['title'][:58]}")
            pain = None
            if r["pain_status"] == "PAIN":
                pain = {k: r[k] for k in ("persona", "job", "workaround", "tools",
                                          "cost_signal", "pay_signal", "quote")}
            d, model = ip._money_call(r["title"], r["meta"], r["source"],
                                      body=r["body"] or "", pain=pain)
            used[model] = used.get(model, 0) + 1
            c = ip.db()
            c.execute("""UPDATE items SET earn_verdict=?, what=COALESCE(NULLIF(what,''), ?),
                         reason=?, first_payment=?, capital=?, proof=?, buyer=?, offer_money=?
                         WHERE id=?""",
                      (d["earn_verdict"], d["what"], d["reason"], d["first_payment"],
                       d["capital"], d.get("proof", ""), d.get("buyer", ""),
                       d.get("offer", ""), r["id"]))
            c.commit(); c.close()
            tick(1)
        label(f"evaluated {len(rows)}")
        return {"evaluated": len(rows), "models": used}

    if async_:
        return {"job": jobs.start(f"Evaluating {limit} opportunities", limit, work)}
    return work(lambda n=1: None, lambda m: None)

@router.get("/money", response_class=HTMLResponse)
def money_page(lane: str = "opportunities", verdict: str = "all", order: str = "best"):
    import jobs
    rows = _rows(lane, verdict, order)
    st = _stats()
    total = sum(st.values())
    go = st.get("GO", 0)
    later = st.get("LATER", 0)
    pend = st.get(None, 0) + st.get("ERROR", 0)

    def link(lane_=None, verdict_=None, order_=None):
        return (f'/money?lane={quote(lane_ or lane)}&verdict={quote(verdict_ or verdict)}'
                f'&order={quote(order_ or order)}')

    def need(n, one, many):
        return f"{n:,} {one if n == 1 else many}"
    lede = (f'{need(go, "offer", "offers")} you could sell this week. '
            f'{need(later, "needs", "need")} more time or an audience; {pend:,} not checked yet.')

    tabs = "".join(
        f'<a class="tab" href="{link(key, "all")}"{" aria-current=page" if lane == key else ""}>{label}</a>'
        for key, label in (("opportunities", "Opportunities"), ("pains", "Verified pains"),
                           ("products", "Buildable products")))

    def opt(kind, key, label):
        cur = {"verdict": verdict, "order": order}[kind] == key
        href = link(verdict_=key) if kind == "verdict" else link(order_=key)
        return f'<a href="{href}"{" aria-current=true" if cur else ""}>{label}</a>'
    filters = ('<div class="filters"><span class="fl">Show</span>'
               + opt("verdict", "all", f"All ({total:,})") + opt("verdict", "go", f"Sell now ({go:,})")
               + opt("verdict", "later", f"Later ({later:,})")
               + opt("verdict", "pending", f"Not checked yet ({pend:,})") + "</div>"
               + '<div class="filters"><span class="fl">Sort by</span>'
               + opt("order", "best", "Best verdict") + opt("order", "quickest", "Quickest money")
               + opt("order", "cheapest", "Cheapest start") + "</div>")

    cards = "".join(_card(r) for r in rows) or (
        '<div class="empty"><p>Nothing in this view yet.</p>'
        '<p>Use <b>Find business ideas</b> to pull posts from r/sidehustle, r/smallbusiness, r/consulting'
        ' and ten more, plus Hacker News comments where people say they would pay for something.</p></div>')

    body = (theme.header("money", "First customer")
            + f'<p class="lede solo">{esc(lede)}</p>'
            + '<p class="how">This page asks one thing: what could you sell this week, to whom, for how much,'
              ' and when does the first payment land? A boring service that sells beats a clever product'
              ' that needs an audience.</p>'
            + '<div class="bar"><button type="button" onclick="scrapeB()">Find business ideas</button>'
              '<button type="button" class="second" onclick="evalB()">Check 12 for first payment</button>'
              '<span id="st" class="say" role="status"></span></div>'
            + f'<nav class="tabs" aria-label="What to sell">{tabs}</nav>'
            + filters
            + f'<main class="list">{cards}</main>'
            + jobs.WIDGET + SCRIPT)
    return HTMLResponse(theme.page("First customer", body, CSS, tab_title="First customer — IdeaPulse"))


CSS = """
.filters+.filters{margin-top:8px}
.list{margin-top:34px}
.entry:last-child{border-bottom:0}
.acts{margin-top:10px}
.lead-link{font-size:13.5px;font-weight:600;color:var(--mark);text-decoration:none;
  border-bottom:1px solid var(--faint);padding-bottom:1px}
.lead-link:hover{border-bottom-color:var(--mark)}
"""

SCRIPT = """<script>
function say(text, tone){
  const s = document.getElementById('st');
  s.textContent = text;
  s.style.color = tone === 'bad' ? 'var(--flag)' : (tone === 'good' ? 'var(--ink)' : 'var(--muted)');
}
async function scrapeB(){
  say('Finding business ideas…');
  const r = await (await fetch('/money/scrape?async_=1')).json();
  JobBar.watch(r.job, (j) => {
    const ok = j.state === 'done';
    say(ok ? (j.result || 'Found') + '. Reloading…' : 'Search failed: ' + (j.message || ''), ok ? 'good' : 'bad');
    if (ok) setTimeout(() => location.reload(), 1400);
  });
}
async function evalB(){
  say('Checking 12 ideas for a first payment…');
  const r = await (await fetch('/money/evaluate?async_=1&limit=12')).json();
  JobBar.watch(r.job, (j) => {
    const ok = j.state === 'done';
    say(ok ? 'Checked. Reloading…' : 'Check failed: ' + (j.message || ''), ok ? 'good' : 'bad');
    if (ok) setTimeout(() => location.reload(), 1400);
  });
}
</script>"""
