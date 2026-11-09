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
import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

BASE = Path(__file__).parent
DB = BASE / "ideas.db"
# sources that describe opportunities rather than already-built products
OPPORTUNITY_SOURCES = ("Reddit business ideas", "HN demand signals")

# launch-directory products that scored BUILD — possible products to sell
PRODUCT_SOURCES = ("Product Hunt", "Fazier", "BuildHop", "Awesome Indie",
                   "OpenHunts", "Uneed")

router = APIRouter()

VERDICT_META = {
    "GO": ("🟢", "GO", "sellable within 2 weeks"),
    "LATER": ("🟡", "LATER", "money, but slower or needs an audience"),
    "NO": ("⚪", "NO", "not a fast first rupee"),
    "ERROR": ("⚠", "RETRY", "evaluation failed"),
}


def esc(s):
    return html.escape(str(s or ""), quote=True)


def _conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(lane, verdict, order):
    conn = _conn()
    marks = ",".join("?" * len(OPPORTUNITY_SOURCES))
    where, params = [], []

    if lane == "opportunities":
        where.append(f"source IN ({marks})")
        params.extend(OPPORTUNITY_SOURCES)
    elif lane == "products":
        m2 = ",".join("?" * len(PRODUCT_SOURCES))
        where.append(f"source IN ({m2})")
        params.extend(PRODUCT_SOURCES)
        where.append("verdict IN ('BUILD','WATCH')")

    if verdict == "go":
        where.append("earn_verdict='GO'")
    elif verdict == "later":
        where.append("earn_verdict='LATER'")
    elif verdict == "pending":
        where.append("(earn_verdict IS NULL OR earn_verdict='ERROR')")

    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    ordsql = {
        "quickest": "ORDER BY CASE WHEN first_payment>0 THEN 0 ELSE 1 END, first_payment, capital",
        "cheapest": "ORDER BY capital, first_payment",
        "hottest": "ORDER BY eng DESC, score DESC",
    }.get(order, "ORDER BY CASE earn_verdict WHEN 'GO' THEN 0 WHEN 'LATER' THEN 1 "
                   "WHEN 'NO' THEN 3 ELSE 2 END, first_payment, eng DESC")
    rows = conn.execute(f"SELECT * FROM items {wsql} {ordsql} LIMIT 120",
                        params).fetchall()
    conn.close()
    return rows


def _stats():
    conn = _conn()
    marks = ",".join("?" * len(OPPORTUNITY_SOURCES))
    rows = conn.execute(f"""SELECT earn_verdict, COUNT(*) c FROM items
                            WHERE source IN ({marks}) GROUP BY earn_verdict""",
                        OPPORTUNITY_SOURCES).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def _card(r):
    def g(k):
        try:
            return r[k]
        except (IndexError, KeyError):
            return None

    v = (g("earn_verdict") or "").upper()
    icon, label, desc = VERDICT_META.get(v, ("⏳", "PENDING", "not evaluated yet"))
    days = g("first_payment") or 0
    cap = g("capital") or 0
    proof = (g("proof") or "").lower()
    proof_badge = {"yes": '<span class="pf yes">✓ earns today</span>',
                   "partial": '<span class="pf part">~ some evidence</span>',
                   "no": '<span class="pf no">no evidence</span>'}.get(proof, "")

    bits = []
    if g("what"):
        bits.append(f'<div class="sell"><b>Sell:</b> {esc(g("what"))}</div>')
    if g("buyer"):
        bits.append(f'<div class="to"><b>To:</b> {esc(g("buyer"))}</div>')
    if g("offer_money"):
        bits.append(f'<div class="price"><b>Price:</b> {esc(g("offer_money"))}</div>')
    detail = "".join(bits)
    reason = f'<div class="rz">💡 {esc(g("reason"))}</div>' if g("reason") else ""
    meta_bits = []
    if days:
        meta_bits.append(f'<span class="pill fast">💰 ~{days}d to first payment</span>')
    if cap == 0:
        meta_bits.append('<span class="pill zero">₹0 to start</span>')
    elif cap:
        meta_bits.append(f'<span class="pill cap">~${cap} to start</span>')
    meta_bits.append(f'<span class="pill src">{esc(g("source"))}</span>')

    return (f'<div class="card {v.lower() or "none"}">'
            f'<div class="head"><span class="vb {v.lower() or "none"}">{icon} {label}</span>'
            f'<span class="vd">{desc}</span></div>'
            f'<div class="title"><a href="{esc(g("url"))}" target="_blank" rel="noopener">'
            f'{esc((g("title") or "")[:150])}</a></div>'
            f'{detail}{reason}'
            f'<div class="meta">{"".join(meta_bits)} {proof_badge}</div>'
            f'<div class="raw">{esc((g("meta") or "")[:220])}</div>'
            f'</div>')


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
    """Score opportunities for the first-currency question."""
    import app as ip
    import jobs

    limit = max(1, min(limit, 40))
    srcs = OPPORTUNITY_SOURCES + PRODUCT_SOURCES

    def work(tick, label):
        conn = ip.db()
        conn.row_factory = sqlite3.Row
        marks = ",".join("?" * len(srcs))
        rows = conn.execute(
            f"""SELECT id, title, meta, source FROM items
                WHERE source IN ({marks}) AND (earn_verdict IS NULL OR earn_verdict='ERROR')
                ORDER BY verdict IS NULL, score DESC, eng DESC LIMIT ?""",
            (*srcs, limit)).fetchall()
        conn.close()
        used = {}
        for i, r in enumerate(rows, 1):
            label(f"{i}/{len(rows)} · {r['title'][:58]}")
            d, model = ip._money_call(r["title"], r["meta"], r["source"])
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
    rows = _rows(lane, verdict, order)
    st = _stats()
    total = sum(st.values())
    go = st.get("GO", 0)
    later = st.get("LATER", 0)
    pend = st.get(None, 0) + st.get("ERROR", 0)

    cards = "".join(_card(r) for r in rows) or (
        '<div class="empty">Nothing here yet.<br><br>'
        '<b>Hit “Scrape business ideas”</b> above — it pulls recent posts from '
        'r/sidehustle, r/juststart, r/smallbusiness, r/consulting and ten more, '
        'plus Hacker News comments where people say they would pay for something.'
        '</div>')

    def chip(kind, value, label, active):
        return (f'<a class="chip {"on" if active == value else ""}" '
                f'href="/money?lane={lane}&verdict={verdict if kind != "verdict" else value}'
                f'&order={value if kind == "order" else order}">{label}</a>')

    lane_chips = (f'<a class="chip {"on" if lane == "opportunities" else ""}" '
                  f'href="/money?lane=opportunities&verdict=all&order={order}">'
                  f'💼 Opportunities</a>'
                  f'<a class="chip {"on" if lane == "products" else ""}" '
                  f'href="/money?lane=products&verdict=all&order={order}">'
                  f'📦 Buildable products</a>')

    v_chips = (f'<a class="chip {"on" if verdict == "all" else ""}" '
               f'href="/money?lane={lane}&verdict=all&order={order}">All ({total})</a>'
               f'<a class="chip {"on" if verdict == "go" else ""}" '
               f'href="/money?lane={lane}&verdict=go&order={order}">🟢 GO ({go})</a>'
               f'<a class="chip {"on" if verdict == "later" else ""}" '
               f'href="/money?lane={lane}&verdict=later&order={order}">🟡 LATER ({later})</a>'
               f'<a class="chip {"on" if verdict == "pending" else ""}" '
               f'href="/money?lane={lane}&verdict=pending&order={order}">⏳ Pending ({pend})</a>')

    o_chips = (f'<a class="chip {"on" if order == "best" else ""}" '
               f'href="/money?lane={lane}&verdict={verdict}&order=best">Best verdict</a>'
               f'<a class="chip {"on" if order == "quickest" else ""}" '
               f'href="/money?lane={lane}&verdict={verdict}&order=quickest">⚡ Quickest money</a>'
               f'<a class="chip {"on" if order == "cheapest" else ""}" '
               f'href="/money?lane={lane}&verdict={verdict}&order=cheapest">₹ Cheapest start</a>')

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>First currency — IdeaPulse</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0b141a;color:#e9edef;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
  padding:26px;max-width:920px;margin:0 auto;line-height:1.5}}
h1{{font-size:25px;font-weight:800;display:inline}}
.navout{{color:#4ade80;font-size:14px;text-decoration:none;margin-left:12px}}
.sub{{color:#8fa3ad;font-size:13.5px;margin:10px 0 16px;max-width:780px}}
.sub b{{color:#e9edef}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 16px}}
.stat{{background:#111b21;border:1px solid #1d2a33;border-radius:11px;padding:10px 15px;min-width:104px}}
.stat .n{{font-size:20px;font-weight:800;line-height:1.2}}
.stat .l{{color:#667781;font-size:11px;text-transform:uppercase;letter-spacing:.5px}}
.stat.go .n{{color:#4ade80}} .stat.lt .n{{color:#fbbf24}} .stat.pd .n{{color:#8fa3ad}}
.bar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}}
button{{background:#008069;color:#fff;border:none;border-radius:20px;padding:9px 18px;
  font-size:13.5px;font-weight:600;cursor:pointer}}
button:hover{{background:#00a884}}
button.alt{{background:#7c3aed}} button.alt:hover{{background:#8b5cf6}}
#st{{color:#4ade80;font-size:12.5px}}
.chips{{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 10px}}
.chip{{background:#111b21;border:1px solid #1d2a33;color:#8fa3ad;border-radius:16px;
  padding:6px 13px;font-size:12.5px;text-decoration:none;font-weight:500}}
.chip:hover{{border-color:#00a884;color:#e9edef}}
.chip.on{{background:#008069;color:#fff;border-color:#008069}}
.card{{background:#111b21;border:1px solid #1d2a33;border-radius:12px;
  padding:13px 16px;margin-bottom:9px}}
.card.go{{border-left:4px solid #4ade80;background:#0a1f16}}
.card.later{{border-left:4px solid #fbbf24;background:#171405}}
.card.no{{opacity:.45;border-left:4px solid #334155}}
.card:hover{{border-color:#00a884}}
.head{{display:flex;align-items:center;gap:9px;margin-bottom:5px}}
.vb{{font-size:11px;font-weight:800;padding:2px 9px;border-radius:9px;letter-spacing:.4px}}
.vb.go{{background:#052e1b;color:#4ade80;border:1px solid #14532d}}
.vb.later{{background:#2e2605;color:#fbbf24;border:1px solid #713f12}}
.vb.no{{background:#1d2a33;color:#94a3b8}}
.vb.none,.vb.error{{background:#1d2a33;color:#667781}}
.vd{{color:#667781;font-size:11.5px}}
.title{{font-size:15px;font-weight:500;line-height:1.4}}
.title a{{color:inherit;text-decoration:none}}
.title a:hover{{color:#4ade80}}
.sell{{color:#a7f3d0;font-size:12.8px;margin-top:6px}}
.sell b{{color:#4ade80}}
.to{{color:#e9edef;font-size:12.5px;margin-top:4px}}
.to b{{color:#93c5fd}}
.price{{color:#fde68a;font-size:12.5px;margin-top:4px}}
.price b{{color:#fbbf24}}
.rz{{color:#93c5fd;font-size:12.5px;margin-top:5px}}
.meta{{margin-top:7px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}}
.pill{{font-size:11px;padding:2px 9px;border-radius:9px;font-weight:700}}
.pill.fast{{background:#052e1b;color:#4ade80}}
.pill.zero{{background:#0a1f16;color:#a7f3d0}}
.pill.cap{{background:#2a1d05;color:#fbbf24}}
.pill.src{{background:#1d2a33;color:#93c5fd;font-weight:600}}
.pf{{font-size:11px;padding:2px 9px;border-radius:9px;font-weight:700}}
.pf.yes{{background:#052e1b;color:#4ade80}}
.pf.part{{background:#2e2605;color:#fbbf24}}
.pf.no{{background:#1d2a33;color:#94a3b8}}
.raw{{color:#4a6270;font-size:11.5px;margin-top:6px;border-top:1px solid #1a2830;padding-top:6px}}
.empty{{color:#8fa3ad;text-align:center;padding:44px 20px;font-size:13.5px;line-height:1.7}}
.empty b{{color:#4ade80}}
@media(max-width:640px){{
 body{{padding:18px 12px 70px}}
 .stat{{min-width:0;flex:1 1 40%}}
 .raw{{display:none}}
 h1{{font-size:21px}}
}}
</style></head><body>
<h1>💰 First currency</h1>
<a class="navout" href="/">← IdeaPulse</a>
<a class="navout" href="/launch?tab=ideas">🚀 Launch dirs</a>
<div class="sub">The single question this board answers: <b>what can you sell this week, to whom,
for how much, and when does the first payment land?</b> Not "is this a good product" — other pages
do that. Rows come from business owners describing what actually pays, and from people on HN
saying they would pay for something. A boring service that sells beats a clever product that
needs an audience.</div>

<div class="stats">
  <div class="stat go"><div class="n">{go}</div><div class="l">GO now</div></div>
  <div class="stat lt"><div class="n">{later}</div><div class="l">Later</div></div>
  <div class="stat pd"><div class="n">{pend}</div><div class="l">Pending</div></div>
  <div class="stat"><div class="n">{total}</div><div class="l">Opportunities</div></div>
</div>

<div class="bar">
  <button onclick="scrapeB()">↻ Scrape business ideas</button>
  <button class="alt" onclick="evalB()">⚡ Evaluate for first ₹</button>
  <span id="st"></span>
</div>

<div class="chips">{lane_chips}</div>
<div class="chips">{v_chips}</div>
<div class="chips">{o_chips}</div>

{cards}
@@JOBBAR@@
<script>
async function scrapeB(){{
  const s = document.getElementById('st');
  s.textContent = ''; s.style.color = '#667781';
  const r = await (await fetch('/money/scrape?async_=1')).json();
  JobBar.watch(r.job, (j) => {{
    const ok = j.state === 'done';
    s.textContent = ok ? (j.result || 'scraped') : ('failed — ' + (j.message || ''));
    s.style.color = ok ? '#4ade80' : '#f87171';
    if (ok) setTimeout(() => location.reload(), 1400);
  }});
}}
async function evalB(){{
  const s = document.getElementById('st');
  s.textContent = ''; s.style.color = '#667781';
  const r = await (await fetch('/money/evaluate?async_=1&limit=12')).json();
  JobBar.watch(r.job, (j) => {{
    const ok = j.state === 'done';
    s.textContent = ok ? 'evaluated — reloading' : ('failed — ' + (j.message || ''));
    s.style.color = ok ? '#4ade80' : '#f87171';
    if (ok) setTimeout(() => location.reload(), 1400);
  }});
}}
</script>
</body></html>""")
