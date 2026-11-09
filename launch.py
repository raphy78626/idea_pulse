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
        reel = ' <span class="tag reel" title="from the 78-places reel">reel</span>' if s.get("from_reel") else ""
        out.append(
            f'<div class="row{" done" if is_done else ""}" data-cat="{esc(cat)}" data-cost="{esc(cost)}" '
            f'data-reel="{1 if s.get("from_reel") else 0}" '
            f'data-done="{1 if is_done else 0}" data-q="{esc((name + " " + s.get("note", "")).lower())}">'
            f'<a class="tick" href="/launch/toggle/{s_slug}?product={quote(product)}" '
            f'title="{"un-mark" if is_done else "mark as launched"}">{"✅" if is_done else "⬜"}</a>'
            f'<span class="num">{i}</span>'
            f'<div class="body"><div class="nm"><a href="{esc(s.get("url"))}" target="_blank" rel="noopener">'
            f'{esc(name)}</a> <span class="tag cat">{esc(cat)}</span>'
            f'<span class="tag cost {esc(cost.replace("+", "p"))}">{esc(cost)}</span>{reel}</div>'
            f'<div class="nt">{esc(s.get("note"))}</div></div>'
            f'<a class="go" href="{esc(s.get("url"))}" target="_blank" rel="noopener">open ↗</a>'
            f'</div>')
    return "\n".join(out)


def _product_options(conn, current):
    rows = conn.execute("""SELECT id, title, verdict FROM items
        WHERE verdict IN ('BUILD','WATCH') ORDER BY buildability DESC, eng DESC, id DESC LIMIT 60""").fetchall()
    opts = ['<option value="">— general (no product) —</option>']
    for r in rows:
        sel = " selected" if str(r[0]) == current else ""
        opts.append(f'<option value="{r[0]}"{sel}>{esc(r[2])} · {esc((r[1] or "")[:70])}</option>')
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
    if v == "BUILD":
        b = r["buildability"] or 0
        col = "#4ade80" if b >= 8 else ("#fbbf24" if b >= 5 else "#f87171")
        tag = f'<span class="v build">🟢 BUILD <b style="color:{col}">{b}/10</b> · ~{r["ttm_days"] or 0}d</span>'
    elif v == "WATCH":
        b = r["buildability"] or 0
        tag = f'<span class="v watch">🟡 WATCH <b>{b}/10</b></span>'
    elif v == "SKIP":
        tag = '<span class="v skip">⚪ SKIP</span>'
    elif v == "ERROR":
        tag = '<span class="v pending">⚠ retry queued</span>'
    else:
        tag = '<span class="v pending">⏳ not evaluated</span>'
    what = f'<div class="iwhat">→ {esc(r["what"])}</div>' if r["what"] else ""
    why = ""
    if v in ("BUILD", "WATCH") and r["reason"]:
        why = f'<div class="iwhy">💡 {esc(r["reason"])}</div>'
    elif v == "SKIP" and r["reason"]:
        why = f'<div class="iwhy skip"> — {esc(r["reason"][:130])}</div>'
    return (f'<div class="icard {v.lower() or "none"}" data-v="{esc(v)}" data-src="{esc(r["source"])}" '
            f'data-q="{esc((r["title"] + " " + (r["meta"] or "")).lower())}">'
            f'<div class="it"><a href="{esc(r["url"])}" target="_blank" rel="noopener">'
            f'{esc(r["title"])}</a> {tag}</div>'
            f'{what}{why}'
            f'<div class="im">{esc(r["source"])} · {esc((r["meta"] or "")[:150])}</div></div>')


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
    cat_chips = '<button class="chip on" data-cat="all">All ({})</button>'.format(total) + "".join(
        f'<button class="chip" data-cat="{esc(c)}">{esc(c)} ({n})</button>'
        for c, n in sorted(cats.items()))
    cat_chips += ('<button class="chip" data-cost="free">💸 Free tier only ({})</button>'.format(free_n))
    cat_chips += f'<button class="chip" data-reel="1">🎬 From the reel ({reel_n})</button>'

    title_html = (f'<div class="cur">Launching: <b>{esc(cur_title)}</b></div>' if cur_title else "")

    # --- ideas tab: source chips + verdict chips + cards
    tot = {k: sum(s.get(k, 0) or 0 for s in stats.values()) for k in ("total", "done", "build", "watch")}
    src_chips = (f'<a class="chip {"on" if source == "all" else ""}" '
                 f'href="/launch?tab=ideas&source=all&verdict={verdict}">'
                 f'All directories ({tot["total"]})</a>')
    for name in SCRAPED:
        st = stats.get(name, {})
        n = st.get("total", 0)
        if not n:
            continue
        src_chips += (f'<a class="chip {"on" if source == name else ""}" '
                      f'href="/launch?tab=ideas&source={quote(name)}&verdict={verdict}">'
                      f'{esc(name)} ({n})</a>')

    vdefs = [("live", "🟢🟡 Build + Watch", tot["build"] + tot["watch"]),
             ("build", "🟢 Build", tot["build"]),
             ("watch", "🟡 Watch", tot["watch"]),
             ("pending", "⏳ Pending", tot["total"] - tot["done"]),
             ("all", "All", tot["total"])]
    v_chips = "".join(
        f'<a class="chip {"on" if verdict == v else ""}" '
        f'href="/launch?tab=ideas&source={quote(source)}&verdict={v}">{lbl} ({n})</a>'
        for v, lbl, n in vdefs)

    idea_cards = "".join(_idea_card(r) for r in ideas) or \
        '<div class="empty">Nothing here yet — hit “Fetch directories” above.</div>'

    # per-directory scorecard
    score_rows = ""
    for name in SCRAPED:
        st = stats.get(name)
        if not st:
            continue
        pctd = round(st["done"] * 100 / st["total"]) if st["total"] else 0
        score_rows += (f'<tr><td><a href="/launch?tab=ideas&source={quote(name)}&verdict=all">'
                       f'{esc(name)}</a></td>'
                       f'<td>{st["total"]}</td>'
                       f'<td><div class="mini"><div class="mini-fill" style="width:{pctd}%"></div></div>'
                       f'<span class="dim">{st["done"]}/{st["total"]} · {pctd}%</span></td>'
                       f'<td class="b">{st["build"]}</td><td class="w">{st["watch"]}</td></tr>')

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Launch directories — IdeaPulse</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0b141a;color:#e9edef;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;padding:26px;max-width:900px;margin:0 auto;line-height:1.5}}
h1{{font-size:25px;font-weight:800;display:inline}}
.navout{{color:#4ade80;font-size:14px;text-decoration:none;margin-left:14px}}
.sub{{color:#667781;font-size:13px;margin:8px 0 16px;max-width:760px}}
.cur{{color:#a7f3d0;font-size:13px;background:#0a1f16;border-left:3px solid #10b981;padding:7px 12px;border-radius:6px;margin-bottom:12px}}
.bar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}}
select,input{{background:#111b21;color:#e9edef;border:1px solid #1d2a33;border-radius:9px;padding:9px 12px;font-size:13.5px;max-width:100%}}
select{{min-width:230px}}
button{{background:#008069;color:#fff;border:none;border-radius:20px;padding:9px 18px;font-size:13.5px;font-weight:600;cursor:pointer}}
button:hover{{background:#00a884}}
.prog{{color:#4ade80;font-size:13px;font-weight:700;margin-left:auto}}
.pbar{{height:8px;background:#111b21;border:1px solid #1d2a33;border-radius:6px;overflow:hidden;margin:6px 0 16px}}
.pfill{{height:100%;background:linear-gradient(90deg,#10b981,#4ade80);width:{pct}%}}
.chips{{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 10px}}
.chip{{background:#111b21;border:1px solid #1d2a33;color:#8fa3ad;border-radius:16px;padding:6px 13px;font-size:12.5px;font-weight:500}}
.chip.on{{background:#008069;color:#fff;border-color:#008069}}
.row{{display:flex;gap:10px;align-items:flex-start;background:#111b21;border:1px solid #1d2a33;border-radius:11px;padding:11px 14px;margin-bottom:7px}}
.row.done{{opacity:.5;border-left:3px solid #10b981}}
.row:hover{{border-color:#00a884}}
.tick{{text-decoration:none;font-size:17px;line-height:1.2;flex:none}}
.num{{color:#4a6270;font-size:11px;min-width:20px;text-align:right;padding-top:3px;flex:none}}
.body{{flex:1;min-width:0}}
.nm{{font-size:15px;font-weight:500}}
.nm a{{color:inherit;text-decoration:none}}
.nm a:hover{{color:#4ade80}}
.tag{{font-size:10.5px;padding:2px 8px;border-radius:9px;margin-left:6px;vertical-align:middle;font-weight:700;text-transform:uppercase;letter-spacing:.3px}}
.tag.cat{{background:#1d2a33;color:#93c5fd}}
.tag.cost{{background:#1d2a33;color:#94a3b8}}
.tag.cost.free{{background:#052e1b;color:#4ade80}}
.tag.cost.freep{{background:#2e2605;color:#fbbf24}}
.tag.cost.paid{{background:#2a1d05;color:#f59e0b}}
.tag.reel{{background:#1a1030;color:#c4b5fd}}
.nt{{color:#8fa3ad;font-size:12.5px;margin-top:3px}}
.go{{color:#4ade80;font-size:11.5px;text-decoration:none;white-space:nowrap;padding-top:4px;flex:none}}
#st{{color:#4ade80;font-size:12.5px}}
.hide{{display:none}}
.tabs{{display:flex;gap:6px;margin:0 0 16px;border-bottom:1px solid #1d2a33}}
.tab{{padding:10px 18px;font-size:14px;font-weight:600;color:#8fa3ad;text-decoration:none;border-bottom:2px solid transparent}}
.tab:hover{{color:#e9edef}}
.tab.on{{color:#4ade80;border-bottom-color:#4ade80}}
.pane{{display:none}}
.pane.on{{display:block}}
.icard{{background:#111b21;border:1px solid #1d2a33;border-radius:11px;padding:12px 15px;margin-bottom:8px}}
.icard.build{{border-left:4px solid #4ade80;background:#0a1f16}}
.icard.watch{{border-left:4px solid #fbbf24;background:#171405}}
.icard.skip{{opacity:.45;border-left:4px solid #334155}}
.icard:hover{{border-color:#00a884}}
.it{{font-size:15px;font-weight:500;line-height:1.4}}
.it a{{color:inherit;text-decoration:none}}
.it a:hover{{color:#4ade80}}
.v{{font-size:10.5px;padding:2px 8px;border-radius:9px;margin-left:8px;font-weight:700;white-space:nowrap;vertical-align:middle}}
.v.build{{background:#052e1b;color:#4ade80;border:1px solid #14532d}}
.v.watch{{background:#2e2605;color:#fbbf24;border:1px solid #713f12}}
.v.skip{{background:#1d2a33;color:#94a3b8}}
.v.pending{{background:#1d2a33;color:#667781}}
.iwhat{{color:#93c5fd;font-size:12.5px;margin-top:5px}}
.iwhy{{color:#a7f3d0;font-size:12px;margin-top:4px}}
.iwhy.skip{{color:#8fa3ad}}
.im{{color:#667781;font-size:11.5px;margin-top:5px}}
.empty{{color:#667781;text-align:center;padding:50px 0}}
table.sc{{width:100%;border-collapse:collapse;font-size:13px;margin:6px 0 18px}}
table.sc th{{text-align:left;color:#93c5fd;font-size:11px;text-transform:uppercase;letter-spacing:.5px;padding:7px 10px;border-bottom:1px solid #22343f;font-weight:600}}
table.sc td{{padding:8px 10px;border-bottom:1px solid #1d2a33}}
table.sc td a{{color:#e9edef;text-decoration:none}}
table.sc td a:hover{{color:#4ade80}}
table.sc td.b{{color:#4ade80;font-weight:700}}
table.sc td.w{{color:#fbbf24;font-weight:700}}
.dim{{color:#4a6270}}
.mini{{height:5px;background:#0b141a;border:1px solid #1d2a33;border-radius:3px;overflow:hidden;margin-bottom:4px;max-width:110px}}
.mini-fill{{height:100%;background:linear-gradient(90deg,#10b981,#4ade80);border-radius:3px}}
.sync{{color:#8fa3ad;font-size:12.5px;margin:0 0 10px}}
@media(max-width:640px){{
 body{{padding:18px 12px 60px}}
 .row{{flex-wrap:wrap}}
 .go{{display:none}}
 select{{width:100%}}
 .prog{{margin-left:0}}
}}
</style></head><body>
<h1>🚀 Launch directories</h1> <a class="navout" href="/">← IdeaPulse</a>
<div class="sub">Two jobs in one place: <b>checklist</b> = where to launch your product · <b>ideas</b> = what
those same directories published and how each one scored. Six of them expose machine-readable data and are
scraped into the normal IdeaPulse pipeline — they are sources like any other feed.</div>
<div class="tabs">
  <a class="tab {"on" if tab == "checklist" else ""}" href="/launch?tab=checklist">✅ Checklist</a>
  <a class="tab {"on" if tab == "ideas" else ""}" href="/launch?tab=ideas">💡 Ideas from these directories</a>
</div>

<div class="pane {"on" if tab == "checklist" else ""}">
{title_html}
<div class="bar">
  <select id="prod" onchange="location.href='/launch?tab=checklist&product='+encodeURIComponent(this.value)">
    {prod_options}
  </select>
  <button onclick="copyLeft()">📋 Copy remaining links</button>
  <span id="st"></span>
  <span class="prog">✅ {done_n} / {total} · {pct}%</span>
</div>
<div class="pbar"><div class="pfill"></div></div>
<div class="chips">{cat_chips}</div>
<div class="bar"><input id="q" placeholder="search a directory or note…" style="flex:1"></div>
<div id="list">
{_rows_html(sites, done, product)}
</div>
</div>

<div class="pane {"on" if tab == "ideas" else ""}">
<div class="sync">Scrape these directories, then evaluate without leaving the page.
<button onclick="syncNow()">↻ Fetch directories</button>
<button onclick="evalNow()" style="background:#7c3aed">⚡ Evaluate new</button>
<span id="ist"></span></div>
<table class="sc">
<thead><tr><th>Directory</th><th>Items</th><th>Evaluated</th><th>🟢 Build</th><th>🟡 Watch</th></tr></thead>
<tbody>{score_rows}</tbody></table>
<div class="chips">{v_chips}</div>
<div class="chips">{src_chips}</div>
<div class="bar"><input id="iq" placeholder="search these ideas…" style="flex:1"></div>
<div id="ideas">{idea_cards}</div>
</div>
<script>
const PRODUCT = {json.dumps(product)};
const chips = [...document.querySelectorAll('.chip')];
let CAT = 'all', FREE = false, REEL = false;
chips.forEach(c => c.onclick = () => {{
  if (c.dataset.cost) {{ FREE = !FREE; c.classList.toggle('on', FREE); }}
  else if (c.dataset.reel) {{ REEL = !REEL; c.classList.toggle('on', REEL); }}
  else {{ CAT = c.dataset.cat; chips.forEach(x => {{ if (!x.dataset.cost && !x.dataset.reel) x.classList.toggle('on', x === c); }}); }}
  apply();
}});
document.getElementById('q').oninput = apply;
function apply(){{
  const q = document.getElementById('q').value.trim().toLowerCase();
  let shown = 0;
  document.querySelectorAll('.row').forEach(r => {{
    const okCat = CAT === 'all' || r.dataset.cat === CAT;
    const okFree = !FREE || r.dataset.cost.startsWith('free');
    const okReel = !REEL || r.dataset.reel === '1';
    const okQ = !q || r.dataset.q.includes(q);
    const show = okCat && okFree && okReel && okQ;
    r.classList.toggle('hide', !show);
    if (show) shown++;
  }});
  document.getElementById('st').textContent = shown + ' shown';
}}
document.querySelectorAll('.tick').forEach(t => {{
  if (!t.href.includes('product=')) t.href += '?product=' + encodeURIComponent(PRODUCT);
}});
async function copyLeft(){{
  const urls = [...document.querySelectorAll('.row')]
    .filter(r => r.dataset.done === '0')
    .map(r => r.querySelector('.go').href);
  const txt = urls.join('\\n');
  try {{ await navigator.clipboard.writeText(txt); }}
  catch {{ prompt('Copy these links:', txt); return; }}
  const s = document.getElementById('st');
  s.textContent = 'copied ' + urls.length + ' links';
  setTimeout(() => s.textContent = '', 2000);
}}
apply();
// --- ideas tab: client-side search over the rendered cards
const iq = document.getElementById('iq');
if (iq) {{
  iq.oninput = () => {{
    const q = iq.value.trim().toLowerCase();
    document.querySelectorAll('.icard').forEach(c => {{
      c.classList.toggle('hide', !!q && !c.dataset.q.includes(q));
    }});
  }};
}}
async function syncNow(){{
  const s = document.getElementById('ist');
  s.textContent = ''; s.style.color = '#667781';
  const r = await (await fetch('/launch/sync?async_=1')).json();
  JobBar.watch(r.job, (j) => {{
    const ok = j.state === 'done';
    s.textContent = ok ? (j.result || 'fetched') : ('failed — ' + (j.message || ''));
    s.style.color = ok ? '#4ade80' : '#f87171';
    if (ok) setTimeout(() => location.reload(), 1200);
  }});
}}
async function evalNow(){{
  const s = document.getElementById('ist');
  s.textContent = ''; s.style.color = '#667781';
  const r = await (await fetch('/launch/evaluate?async_=1&limit=10')).json();
  JobBar.watch(r.job, (j) => {{
    const ok = j.state === 'done';
    s.textContent = ok ? 'evaluated — reloading' : ('failed — ' + (j.message || ''));
    s.style.color = ok ? '#4ade80' : '#f87171';
    if (ok) setTimeout(() => location.reload(), 1200);
  }});
}}
</script>
@@JOBBAR@@
</body></html>"""
    jobs_widget = (__import__("jobs").WIDGET) if tab == "ideas" else ""
    return HTMLResponse(body.replace("@@JOBBAR@@", jobs_widget))


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
