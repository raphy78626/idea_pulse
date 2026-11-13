"""Launch directories — two jobs in one page.

1. Checklist: where to launch a product (launch_sites.json, every URL verified).
2. Ideas: what those same directories *published*, and how each one scored.

The six directories that expose machine-readable data are scraped by
launch_scrape.py into `items`, so they are sources like any other feed. This
page shows the results and lets you trigger evaluation without leaving it.

Tick-off state is per product, so the same list can be worked for several ideas.
"""
import html
import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

import theme

BASE = Path(__file__).parent
DB = BASE / "ideas.db"
SITES_FILE = BASE / "launch_sites.json"

# these six directories are scraped into `items` (see launch_scrape.py)
SCRAPED = ("Product Hunt", "Fazier", "BuildHop", "Awesome Indie", "OpenHunts", "Uneed")

router = APIRouter()


def _db():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS launch_done (
        site TEXT, product TEXT DEFAULT '', done_on TEXT,
        PRIMARY KEY (site, product))""")
    conn.commit()
    return conn


def _sites():
    try:
        return json.loads(SITES_FILE.read_text())
    except (OSError, ValueError):
        return []


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def esc(s):
    return html.escape(str(s or ""), quote=True)


def _rows_html(sites, done, product=""):
    out = []
    for i, s in enumerate(sites, 1):
        name = s.get("name", "")
        s_slug = slug(name)
        cost = s.get("cost", "free")
        cat = s.get("category", "")
        is_done = s_slug in done
        kind = ", ".join(x for x in (cat, cost, "from the reel" if s.get("from_reel") else "") if x)
        out.append(
            f'<div class="row{" done" if is_done else ""}" data-cat="{esc(cat)}" data-cost="{esc(cost)}" '
            f'data-reel="{1 if s.get("from_reel") else 0}" '
            f'data-done="{1 if is_done else 0}" data-q="{esc((name + " " + s.get("note", "")).lower())}">'
            f'<a class="tick" href="/launch/toggle/{s_slug}?product={quote(product)}" role="checkbox" '
            f'aria-checked="{"true" if is_done else "false"}" '
            f'aria-label="{"Mark " + esc(name) + " as not launched" if is_done else "Mark " + esc(name) + " as launched"}">'
            f'</a>'
            f'<span class="num">{i}</span>'
            f'<div class="body"><div class="nm"><a href="{esc(s.get("url"))}" target="_blank" rel="noopener">'
            f'{esc(name)}</a><span class="kind">{esc(kind)}</span></div>'
            f'<div class="nt">{esc(s.get("note"))}</div></div>'
            f'<a class="go" href="{esc(s.get("url"))}" target="_blank" rel="noopener">Open</a>'
            f'</div>')
    return "\n".join(out)


def _product_options(conn, current):
    rows = conn.execute("""SELECT id, title, verdict FROM items
        WHERE verdict IN ('BUILD','WATCH') ORDER BY buildability DESC, eng DESC, id DESC LIMIT 60""").fetchall()
    opts = ['<option value="">General, no product</option>']
    for r in rows:
        sel = " selected" if str(r[0]) == current else ""
        opts.append(f'<option value="{r[0]}"{sel}>{esc((r[1] or "")[:70])} ({esc((r[2] or "").title())})</option>')
    return "".join(opts)


SOURCE_ORDER = ("CASE source WHEN 'Product Hunt' THEN 0 WHEN 'Fazier' THEN 1 "
                "WHEN 'Uneed' THEN 2 WHEN 'BuildHop' THEN 3 WHEN 'Awesome Indie' THEN 4 "
                "WHEN 'OpenHunts' THEN 5 ELSE 6 END")
VERDICT_ORDER = ("CASE verdict WHEN 'BUILD' THEN 0 WHEN 'WATCH' THEN 1 "
                 "WHEN 'SKIP' THEN 3 ELSE 2 END")


def _stats(conn):
    """Per-directory counts: total, evaluated, and how many are BUILD/WATCH."""
    marks = ",".join("?" * len(SCRAPED))
    rows = conn.execute(
        f"""SELECT source, COUNT(*) total,
                   SUM(CASE WHEN verdict IS NOT NULL AND verdict!='ERROR' THEN 1 ELSE 0 END) done,
                   SUM(CASE WHEN verdict='BUILD' THEN 1 ELSE 0 END) build,
                   SUM(CASE WHEN verdict='WATCH' THEN 1 ELSE 0 END) watch
            FROM items WHERE source IN ({marks}) GROUP BY source""",
        SCRAPED).fetchall()
    return {r[0]: {"total": r[1], "done": r[2], "build": r[3], "watch": r[4]} for r in rows}


def _ideas(conn, source="all", verdict="live", limit=60):
    marks = ",".join("?" * len(SCRAPED))
    where = [f"source IN ({marks})"]
    params = list(SCRAPED)
    if source != "all":
        where.append("source=?")
        params.append(source)
    if verdict == "live":
        where.append("verdict IN ('BUILD','WATCH')")
    elif verdict == "build":
        where.append("verdict='BUILD'")
    elif verdict == "watch":
        where.append("verdict='WATCH'")
    elif verdict == "skip":
        where.append("verdict='SKIP'")
    elif verdict == "pending":
        where.append("(verdict IS NULL OR verdict='ERROR')")
    # buildability first so the best ideas are never buried, then heat
    rows = conn.execute(
        f"""SELECT id, source, title, url, meta, verdict, buildability, ttm_days,
                   what, reason, nutshell
            FROM items WHERE {' AND '.join(where)}
            ORDER BY {VERDICT_ORDER}, buildability DESC, eng DESC, id DESC LIMIT ?""",
        params + [limit]).fetchall()
    return rows


def _idea_card(r):
    v = r["verdict"] or ""
    b = r["buildability"] or 0
    days = r["ttm_days"]
    if v in ("BUILD", "WATCH"):
        ship = ""
        if days not in (None, ""):
            ship = ", could ship within a day" if int(days) <= 1 else f", about {days} days to ship"
        line = f'<span class="vw">{v.title()}.</span> Buildable {b} of 10{ship}.'
    elif v == "SKIP":
        line = '<span class="vw">Skipped.</span> ' + esc((r["reason"] or "No reason given")[:140])
    elif v == "ERROR":
        line = '<span class="vw">Evaluation failed, will retry.</span>'
    else:
        line = '<span class="vw">Not read yet.</span>'
    tier = {"BUILD": "hi", "WATCH": "lo"}.get(v, "off")
    facts = "".join(f"<dt>{label}</dt><dd>{esc(val)}</dd>" for label, val in (
        ("Idea", r["what"]), ("Why", r["reason"] if v in ("BUILD", "WATCH") else "")) if val)
    meta = (r["meta"] or "").replace(" · ", ", ")
    meta = meta if len(meta) <= 140 else meta[:137].rstrip(" ,") + "…"
    return (f'<article class="entry icard {tier}" data-v="{esc(v)}" data-src="{esc(r["source"])}" '
            f'data-q="{esc((r["title"] + " " + (r["meta"] or "")).lower())}">'
            f'<h2><a class="tl" href="{esc(r["url"])}" target="_blank" rel="noopener">'
            f'<mark>{esc(r["title"])}</mark></a></h2>'
            f'<p class="verdict">{line}</p>'
            + (f'<dl class="facts">{facts}</dl>' if facts else "")
            + f'<p class="meta">{esc(", ".join(x for x in (r["source"], meta) if x))}</p></article>')


@router.get("/launch", response_class=HTMLResponse)
def launch_page(product: str = "", tab: str = "checklist",
                source: str = "all", verdict: str = "live"):
    sites = sorted(_sites(), key=lambda s: s.get("priority", 999))
    conn = _db()
    conn.row_factory = sqlite3.Row
    done = {r[0] for r in conn.execute("SELECT site FROM launch_done WHERE product=?", (product,))}
    prod_options = _product_options(conn, product)
    cur_title = ""
    if product.isdigit():
        row = conn.execute("SELECT title FROM items WHERE id=?", (int(product),)).fetchone()
        cur_title = (row[0] if row else "")[:120]
    stats = _stats(conn)
    ideas = _ideas(conn, source=source, verdict=verdict)
    conn.close()

    total = len(sites)
    done_n = sum(1 for s in sites if slug(s.get("name")) in done)
    free_n = sum(1 for s in sites if s.get("cost", "free").startswith("free"))
    reel_n = sum(1 for s in sites if s.get("from_reel"))
    pct = round(done_n * 100 / total) if total else 0
    cats = {}
    for s in sites:
        cats[s.get("category", "")] = cats.get(s.get("category", ""), 0) + 1
    cat_chips = (f'<button type="button" class="chip" data-cat="all" aria-pressed="true">All ({total})</button>'
                 + "".join(f'<button type="button" class="chip" data-cat="{esc(c)}" aria-pressed="false">'
                           f'{esc(c)} ({n})</button>' for c, n in sorted(cats.items())))
    extra_chips = (f'<button type="button" class="chip" data-cost="free" aria-pressed="false">'
                   f'Free tier only ({free_n})</button>'
                   f'<button type="button" class="chip" data-reel="1" aria-pressed="false">'
                   f'From the reel ({reel_n})</button>')

    # --- ideas tab
    tot = {k: sum(s.get(k, 0) or 0 for s in stats.values()) for k in ("total", "done", "build", "watch")}

    def ilink(source_=None, verdict_=None):
        return (f'/launch?tab=ideas&source={quote(source_ or source)}'
                f'&verdict={quote(verdict_ or verdict)}')
    src_opts = (f'<a href="{ilink("all")}"{" aria-current=true" if source == "all" else ""}>'
                f'All ({tot["total"]:,})</a>')
    for name in SCRAPED:
        n = stats.get(name, {}).get("total", 0)
        if n:
            src_opts += (f'<a href="{ilink(name)}"{" aria-current=true" if source == name else ""}>'
                         f'{esc(name)} ({n:,})</a>')
    vdefs = [("live", "Build and watch", tot["build"] + tot["watch"]), ("build", "Build", tot["build"]),
             ("watch", "Watch", tot["watch"]), ("pending", "Not read yet", tot["total"] - tot["done"]),
             ("all", "All", tot["total"])]
    v_opts = "".join(f'<a href="{ilink(verdict_=v)}"{" aria-current=true" if verdict == v else ""}>'
                     f'{lbl} ({n:,})</a>' for v, lbl, n in vdefs)

    idea_cards = "".join(_idea_card(r) for r in ideas) or (
        '<div class="empty"><p>Nothing in this view yet.</p>'
        '<p>Use <b>Fetch directories</b> to pull what these directories published.</p></div>')

    score_rows = ""
    for name in SCRAPED:
        st = stats.get(name)
        if not st:
            continue
        pctd = round(st["done"] * 100 / st["total"]) if st["total"] else 0
        score_rows += (f'<tr><th scope="row"><a href="{ilink(name, "all")}">{esc(name)}</a></th>'
                       f'<td>{st["total"]:,}</td>'
                       f'<td><span class="meter"><span style="width:{pctd}%"></span></span>{pctd}%</td>'
                       f'<td>{st["build"]}</td><td>{st["watch"]}</td></tr>')

    if tab == "ideas":
        lede = (f'{tot["build"]} Build and {tot["watch"]} Watch ideas came from these directories. '
                f'{tot["total"] - tot["done"]:,} not read yet.')
        how = ('Six of these directories publish machine-readable lists, so IdeaPulse reads them like any '
               'other feed. Fetch them and evaluate here without leaving the page.')
    else:
        lede = f'{done_n} of {total} directories done' + (f' for {cur_title}.' if cur_title else '.')
        how = ('Pick the product you are launching, then tick each directory once you have posted it. '
               'Ticks are saved per product, so the same list works for several ideas.')

    tabs = (f'<a class="tab" href="/launch?tab=checklist&product={quote(product)}"'
            f'{" aria-current=page" if tab != "ideas" else ""}>Checklist</a>'
            f'<a class="tab" href="/launch?tab=ideas"{" aria-current=page" if tab == "ideas" else ""}>'
            f'Ideas from these directories</a>')

    checklist = f"""
<div class="bar">
  <label for="prod">Launching</label>
  <select id="prod" onchange="location.href='/launch?tab=checklist&product='+encodeURIComponent(this.value)">{prod_options}</select>
  <button type="button" class="second" onclick="copyLeft()">Copy links not done yet</button>
  <span id="st" class="say" role="status"></span>
</div>
<div class="progress" role="progressbar" aria-valuenow="{pct}" aria-valuemin="0" aria-valuemax="100"
  aria-label="Directories done"><span style="width:{pct}%"></span></div>
<div class="filters"><span class="fl">Type</span>{cat_chips}</div>
<div class="filters"><span class="fl">Only</span>{extra_chips}</div>
<input id="q" type="search" placeholder="Search a directory or note" aria-label="Search directories">
<div id="list">{_rows_html(sites, done, product)}</div>"""

    ideas_pane = f"""
<div class="bar">
  <button type="button" onclick="syncNow()">Fetch directories</button>
  <button type="button" class="second" onclick="evalNow()">Evaluate 10 more</button>
  <span id="ist" class="say" role="status"></span>
</div>
<table class="sc">
<thead><tr><th scope="col">Directory</th><th scope="col">Ideas</th><th scope="col">Read so far</th>
<th scope="col">Build</th><th scope="col">Watch</th></tr></thead>
<tbody>{score_rows}</tbody></table>
<div class="filters"><span class="fl">Show</span>{v_opts}</div>
<div class="filters"><span class="fl">Directory</span>{src_opts}</div>
<input id="iq" type="search" placeholder="Search these ideas" aria-label="Search ideas">
<div id="ideas">{idea_cards}</div>"""

    body = (theme.header("launch", "Launch directories")
            + f'<p class="lede solo">{esc(lede)}</p><p class="how">{how}</p>'
            + f'<nav class="tabs" aria-label="Sections">{tabs}</nav>'
            + (ideas_pane if tab == "ideas" else checklist)
            + SCRIPT.replace("@@PRODUCT@@", json.dumps(product))
            + ((__import__("jobs").WIDGET) if tab == "ideas" else ""))
    return HTMLResponse(theme.page("Launch directories", body, CSS,
                                   tab_title="Launch directories — IdeaPulse"))


CSS = """
.bar label{font-size:14px}
.bar select{min-width:260px}
.progress{height:4px;background:var(--rule);border-radius:2px;margin:22px 0 0;max-width:66ch;overflow:hidden}
.progress span{display:block;height:100%;background:var(--mark)}
.filters+.filters{margin-top:8px}
#q,#iq{width:100%;max-width:66ch;margin:22px 0 10px}
.hide{display:none!important}

.row{display:grid;grid-template-columns:22px 26px minmax(0,1fr) auto;gap:2px 12px;align-items:start;
  max-width:66ch;padding:14px 0;border-bottom:1px solid var(--rule)}
.tick{width:20px;height:20px;border:1.5px solid var(--faint);border-radius:4px;margin-top:3px;display:block;
  text-decoration:none;position:relative}
.tick:hover{border-color:var(--ink)}
.row.done .tick{background:var(--mark);border-color:var(--mark)}
.row.done .tick::after{content:"";position:absolute;left:5px;top:1px;width:6px;height:11px;
  border:solid var(--ground);border-width:0 2px 2px 0;transform:rotate(45deg)}
.num{color:var(--faint);font-size:13px;text-align:right;padding-top:4px;font-variant-numeric:tabular-nums}
.nm{font:400 18px/1.4 var(--serif)}
.nm a{text-decoration:none}
.nm a:hover{text-decoration:underline;text-underline-offset:3px}
.kind{font:14px/1.4 var(--sans);color:var(--muted);margin-left:10px}
.nt{color:var(--muted);font-size:14.5px;margin-top:2px}
.row.done .nm,.row.done .nt{color:var(--faint)}
.go{font-size:14px;color:var(--muted);padding-top:4px;text-decoration:underline;
  text-decoration-color:var(--faint);text-underline-offset:3px}
.go:hover{color:var(--ink)}

table.sc{border-collapse:collapse;margin:30px 0 10px;font-size:14.5px;width:100%;max-width:66ch}
table.sc th,table.sc td{text-align:left;padding:8px 14px 8px 0;border-bottom:1px solid var(--rule);font-weight:400}
table.sc thead th{color:var(--muted);font-size:13.5px}
table.sc tbody th a{text-decoration:none}
table.sc tbody th a:hover{text-decoration:underline}
table.sc td{font-variant-numeric:tabular-nums}
.meter{display:inline-block;width:70px;height:4px;background:var(--rule);border-radius:2px;margin-right:8px;
  vertical-align:middle;overflow:hidden}
.meter span{display:block;height:100%;background:var(--ink)}
#ideas{margin-top:24px}
#ideas .entry:last-child{border-bottom:0}

@media (max-width:860px){
  .bar select{min-width:0;width:100%}
  .row{grid-template-columns:22px minmax(0,1fr)}
  .num,.go{display:none}
  .kind{display:block;margin-left:0}
  table.sc th:nth-child(3),table.sc td:nth-child(3){display:none}
}
"""

SCRIPT = """<script>
const PRODUCT = @@PRODUCT@@;
const chips = [...document.querySelectorAll('.chip')];
let CAT = 'all', FREE = false, REEL = false;
chips.forEach(c => c.onclick = () => {
  if (c.dataset.cost) { FREE = !FREE; c.setAttribute('aria-pressed', FREE); }
  else if (c.dataset.reel) { REEL = !REEL; c.setAttribute('aria-pressed', REEL); }
  else {
    CAT = c.dataset.cat;
    chips.forEach(x => { if (!x.dataset.cost && !x.dataset.reel) x.setAttribute('aria-pressed', x === c); });
  }
  apply();
});
const q = document.getElementById('q');
if (q) q.oninput = apply;
function apply(){
  if (!q) return;
  const term = q.value.trim().toLowerCase();
  let shown = 0;
  document.querySelectorAll('.row').forEach(r => {
    const show = (CAT === 'all' || r.dataset.cat === CAT) && (!FREE || r.dataset.cost.startsWith('free'))
      && (!REEL || r.dataset.reel === '1') && (!term || r.dataset.q.includes(term));
    r.classList.toggle('hide', !show);
    if (show) shown++;
  });
  document.getElementById('st').textContent = shown + ' shown';
}
document.querySelectorAll('.tick').forEach(t => {
  if (!t.href.includes('product=')) t.href += '?product=' + encodeURIComponent(PRODUCT);
});
async function copyLeft(){
  const urls = [...document.querySelectorAll('.row')]
    .filter(r => r.dataset.done === '0')
    .map(r => r.querySelector('.go').href);
  const txt = urls.join('\\n');
  try { await navigator.clipboard.writeText(txt); }
  catch { prompt('Copy these links:', txt); return; }
  const s = document.getElementById('st');
  s.textContent = 'Copied ' + urls.length + ' links';
  setTimeout(() => apply(), 2000);
}
apply();
const iq = document.getElementById('iq');
if (iq) {
  iq.oninput = () => {
    const term = iq.value.trim().toLowerCase();
    document.querySelectorAll('.icard').forEach(c => c.classList.toggle('hide', !!term && !c.dataset.q.includes(term)));
  };
}
function say(id, text, tone){
  const s = document.getElementById(id);
  s.textContent = text;
  s.style.color = tone === 'bad' ? 'var(--flag)' : (tone === 'good' ? 'var(--ink)' : 'var(--muted)');
}
async function syncNow(){
  say('ist', 'Fetching directories…');
  const r = await (await fetch('/launch/sync?async_=1')).json();
  JobBar.watch(r.job, (j) => {
    const ok = j.state === 'done';
    say('ist', ok ? (j.result || 'Fetched') + '. Reloading…' : 'Fetch failed: ' + (j.message || ''), ok ? 'good' : 'bad');
    if (ok) setTimeout(() => location.reload(), 1200);
  });
}
async function evalNow(){
  say('ist', 'Evaluating 10 ideas…');
  const r = await (await fetch('/launch/evaluate?async_=1&limit=10')).json();
  JobBar.watch(r.job, (j) => {
    const ok = j.state === 'done';
    say('ist', ok ? 'Evaluated. Reloading…' : 'Evaluation failed: ' + (j.message || ''), ok ? 'good' : 'bad');
    if (ok) setTimeout(() => location.reload(), 1200);
  });
}
</script>"""


@router.get("/launch/toggle/{site_slug}")
def launch_toggle(site_slug: str, product: str = ""):
    conn = _db()
    row = conn.execute("SELECT 1 FROM launch_done WHERE site=? AND product=?",
                       (site_slug, product)).fetchone()
    if row:
        conn.execute("DELETE FROM launch_done WHERE site=? AND product=?", (site_slug, product))
    else:
        conn.execute("INSERT INTO launch_done (site, product, done_on) VALUES (?,?,?)",
                     (site_slug, product, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()
    return RedirectResponse("/launch?product=" + quote(product), status_code=303)


@router.post("/launch/sync")
@router.get("/launch/sync")
def launch_sync(async_: int = 0):
    """Scrape just the six launch directories (not the whole IdeaPulse feed)."""
    import httpx
    import app as ip  # lazy: app imports this module's router at startup
    import jobs
    import launch_scrape

    def work(tick, label):
        total, report = 0, []
        srcs = list(launch_scrape.ADAPTERS.items())
        with httpx.Client(headers=ip.UA, timeout=25, follow_redirects=True) as client:
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
        label(f"{total} new items")
        return {"new_items": total, "sources": report}

    if async_:
        return {"job": jobs.start("Scraping launch directories", len(launch_scrape.ADAPTERS), work)}
    return work(lambda n=1: None, lambda msg: None)


@router.post("/launch/evaluate")
@router.get("/launch/evaluate")
def launch_evaluate(limit: int = 10, async_: int = 0):
    """Evaluate only items that came from the scraped directories.

    app.evaluate_items() orders by score across every pending item, so these
    would otherwise sit behind thousands of older ones indefinitely.
    """
    import app as ip
    import jobs

    limit = max(1, min(limit, 40))
    marks = ",".join("?" * len(SCRAPED))

    def pending():
        conn = ip.db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT id, title, meta FROM items
                WHERE source IN ({marks}) AND (verdict IS NULL OR verdict='ERROR')
                ORDER BY score DESC, eng DESC LIMIT ?""",
            (*SCRAPED, limit)).fetchall()
        conn.close()
        return rows

    def work(tick, label):
        rows = pending()
        used = {}
        for i, r in enumerate(rows, 1):
            label(f"{i}/{len(rows)} · {r['title'][:60]}")
            d, model = ip._eval_call(r["title"], r["meta"])
            used[model] = used.get(model, 0) + 1
            c = ip.db()
            c.execute("""UPDATE items SET verdict=?, buildability=?, ttm_days=?, what=?, reason=?
                         WHERE id=?""",
                      (d["verdict"], d["buildability"], d["ttm_days"],
                       d.get("what", "")[:200], d.get("reason", "")[:200], r["id"]))
            c.commit()
            c.close()
            tick(1)
        label(f"evaluated {len(rows)} ideas")
        return {"evaluated": len(rows), "models": used}

    if async_:
        total = min(limit, len(pending()))
        return {"job": jobs.start(f"Evaluating {total} launch ideas", total, work)}
    return work(lambda n=1: None, lambda msg: None)
