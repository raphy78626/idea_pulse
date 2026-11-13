"""/simulations — what a MiroFish run is, in plain words, shown on your own runs.

Written for product owners and engineers who have never seen MiroFish. Every
example on the page is read live from a real run in ideas.db, and every number
in the engineers' section is read from the code, so the page cannot drift from
what actually happens.
"""
import sqlite3
import sys
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import mirofish_bridge
import sim_explain
import theme

BASE = Path(__file__).parent
DB = BASE / "ideas.db"
esc = theme.esc

router = APIRouter()


def _app_setting(name, default):
    """A constant from app.py without re-importing it (it may be running as __main__)."""
    for mod in (sys.modules.get("app"), sys.modules.get("__main__")):
        if mod is not None and hasattr(mod, name):
            return getattr(mod, name)
    return default


def _runs():
    """Every item with a saved simulation, with its report and checks."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        if "sim_path" not in cols:
            return []
        rows = conn.execute("SELECT id, title, what, source, sim_path FROM items"
                            " WHERE sim_path IS NOT NULL AND sim_path != '' ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        report = mirofish_bridge.read_report(r["sim_path"])
        if not report:
            continue
        idea = ((r["what"] or "") + " " + (r["title"] or "")).strip()
        out.append({"id": r["id"], "title": r["title"] or "", "what": r["what"] or "",
                    "report": report, "idea": idea,
                    "verdict": sim_explain.run_verdict(report, idea),
                    "explain": sim_explain.explain(report)})
    return out


def _steps(example):
    """The four stages of a run, each shown on a real run when there is one."""
    def ex(text):
        return f'<p class="ex">{text}</p>' if example else ""
    e = example
    cast = ", ".join((e["explain"]["cast"] if e else [])[:6])
    quotes = sim_explain.example_quotes(e["report"], e["idea"], 1) if e else []
    stages = {k: label for k, _, _, label in mirofish_bridge.STAGES}
    return f"""
<ol class="steps">
  <li><h3>Describe the idea</h3>
    <p>IdeaPulse writes a short brief from the post and its evaluation: what the product is, who it
    is for, and which real companies it competes with. It asks the personas one question:
    “{esc(mirofish_bridge.SIM_REQUIREMENT)}”</p>
    {ex(f'Example, from the run on “{esc(e["title"])}”: the brief described '
        f'<i>{esc(e["what"] or e["title"])}</i>.') if e else ""}</li>
  <li><h3>Map the market</h3>
    <p>An AI reads the brief and lists the people and companies involved, then links them in a
    knowledge graph: who sells to whom, who competes with whom. ({esc(stages.get("ontology", ""))},
    then {esc(stages.get("build", ""))}.)</p>
    {ex(f'It named: {esc(cast)}.') if e and cast else ""}</li>
  <li><h3>Role-play</h3>
    <p>Each participant becomes an AI persona. Over up to {mirofish_bridge.MAX_ROUNDS} rounds they
    post, reply and react to the announcement, the way people would on a social network.</p>
    {ex(f'One fact the personas worked from: “{esc(quotes[0])}”') if quotes else ""}</li>
  <li><h3>Write it up</h3>
    <p>A final AI reads everything the personas did and writes a report. This is the page you open
    from <b>Simulate buyers</b>. A full run takes about 6 to 11 minutes.</p>
    {ex(f'That report ran to {e["explain"]["words"]:,} words.') if e else ""}</li>
</ol>"""


def _run_entry(run):
    v = run["verdict"]
    if v["kind"] == "off_topic":
        q, lead = sim_explain.example_quotes(run["report"], "", 1), "What it talked about instead"
    elif v["kind"] == "thin":
        q, lead = sim_explain.example_quotes(run["report"], "", 1), "Most of what it had"
    else:
        q, lead = sim_explain.example_quotes(run["report"], run["idea"], 1), "A useful line"
    quote = f'<p class="q"><span>{lead}:</span> “{esc(q[0])}”</p>' if q else ""
    return (f'<article class="run {v["kind"]}">'
            f'<h3><a href="/simulation/{run["id"]}">{esc(run["title"])}</a></h3>'
            f'<p class="vd"><b>{esc(v["label"])}.</b> {esc(v["say"])}</p>{quote}</article>')


@router.get("/simulations", response_class=HTMLResponse)
def simulations_guide():
    runs = _runs()
    usable = [r for r in runs if r["verdict"]["kind"] == "usable"]
    example = max(usable, key=lambda r: r["verdict"]["thin"]["facts"]) if usable else None
    counts = {k: sum(1 for r in runs if r["verdict"]["kind"] == k) for k in ("usable", "off_topic", "thin")}

    if runs:
        tally = (f'{len(runs)} run{"" if len(runs) == 1 else "s"} so far: {counts["usable"]} stayed on the idea, '
                 f'{counts["off_topic"]} went off topic and {counts["thin"]} had too little to go on.')
        run_list = "".join(_run_entry(r) for r in runs)
    else:
        tally = "No simulations have run yet."
        run_list = ('<p class="empty">No simulations have run yet. Pick a Build idea on the '
                    '<a href="/">Ideas</a> page and choose <b>Simulate buyers</b>.</p>')

    headlines = [r["explain"]["headline"] for r in runs if r["explain"]["headline"]]
    same = ""
    if len(headlines) >= 2:
        same = ('<section><h2>Why every summary line sounds the same</h2>'
                '<p>Each report opens with a one-line summary. Here they are, from different ideas:</p>'
                '<ul class="heads">' + "".join(f"<li>{esc(h)}</li>" for h in headlines) + '</ul>'
                '<p>They say almost the same thing whatever the idea, so they tell you little. '
                'Skip the summary and read the quoted facts and the named rivals.</p></section>')

    attempts = _app_setting("SIM_MAX_ATTEMPTS", 2)
    sources = ", ".join(_app_setting("SIMULATABLE_SOURCES", ()))
    stage_list = "".join(f"<li><code>{esc(k)}</code>: {esc(label)} ({a}–{b}% of the progress bar)</li>"
                         for k, a, b, label in mirofish_bridge.STAGES)

    body = f"""
{theme.header("simulations", "Simulations")}
<p class="lede solo">A simulation is a rehearsal, not a forecast.</p>
<p class="how">AI personas act out how buyers, rivals and critics might react to your idea, so you can
hear objections before you talk to real people. Nobody real is asked, and nothing is measured.
{esc(tally)}</p>
<p class="paused">Paused since 13 November 2026: simulations no longer run every morning, because none
has yet been checked against real replies from outreach. You can still run one by hand.</p>

<section><h2>How one run works</h2>{_steps(example)}</section>

<section><h2>Your runs, checked</h2>
<p>Every report is checked two ways before you read it. <b>Did it stay on the idea?</b> It must keep
using the idea's own key words. <b>Did it have enough to go on?</b> It needs at least
{sim_explain.THIN_FACTS} quoted facts, and must not admit it had little.</p>
{run_list}</section>

{same}

<section class="two"><div><h2>Use it for</h2><ul>
<li>Hearing objections you had not thought of</li>
<li>Spotting which rival feels threatened, and how</li>
<li>Finding where buyers gather, so you know where to pitch</li>
<li>Testing the wording of a pitch before you send it</li></ul></div>
<div><h2>Don't use it as proof of</h2><ul>
<li>Whether anyone will pay</li>
<li>Market size, growth or timing</li>
<li>What a real company will actually do next</li>
<li>Anything, if the check above says it went off topic</li></ul></div></section>

<section><h2>Words you'll see</h2><dl class="facts gloss">
<dt>Brief</dt><dd>The short description of the idea the run starts from (the code calls it the seed).</dd>
<dt>Market map</dt><dd>The list of people and companies involved, and their types (the ontology).</dd>
<dt>Knowledge graph</dt><dd>Those people and companies, joined by who sells to, uses or competes with whom.</dd>
<dt>Persona</dt><dd>An AI playing one participant, such as a buyer, a rival or a critic.</dd>
<dt>Round</dt><dd>One turn in which every persona can post or reply.</dd>
<dt>Report</dt><dd>The write-up of what the personas did. It often says “will”; read that as “did, in this run”.</dd>
</dl></section>

<section><details><summary><h2>For engineers</h2></summary>
<p>IdeaPulse does not call MiroFish's HTTP API directly. <code>mirofish_bridge.py</code> runs the
foresight skill's command-line tool, one stage at a time:</p>
<ul>{stage_list}</ul>
<ul>
<li>Each stage times out after {mirofish_bridge.STAGE_TIMEOUT_S} seconds; any failure raises
<code>BridgeError</code> and is recorded, never skipped.</li>
<li>The brief must name at least {mirofish_bridge.MIN_NAMED_ACTORS} named participants, or the run stops before it starts.</li>
<li>Role-play runs for at most {mirofish_bridge.MAX_ROUNDS} rounds.</li>
<li>A report shorter than {mirofish_bridge.MIN_REPORT_CHARS} characters, or one that ends mid-sentence,
counts as a failure. Leaked model reasoning is stripped before saving.</li>
<li>Each idea gets at most {attempts} attempts, counting failures, so a broken idea stops using up time.</li>
<li>Only ideas from {esc(sources) or "problem-describing sources"} rated Build are picked for the
daily queue. Launch feeds announce things that already exist, so there is nothing to react to.</li>
<li>Reports are saved as <code>evaluations/sim_&lt;id&gt;.md</code>. The checks on this page live in
<code>sim_explain.py</code> (<code>topic_check</code>, <code>thin_check</code>, <code>run_verdict</code>).</li>
</ul>
<p>Commands, run from the project folder:</p>
<pre><code>python app.py simulate --dry-run   # what would run, without running it
python app.py simulate 3           # run up to 3 now
mirofish_server.sh start           # MiroFish does not start on its own after a reboot</code></pre>
</details></section>
"""
    css = """
section{margin-top:44px;max-width:70ch}
section h2{font:500 26px/1.25 var(--serif);margin-bottom:12px}
section p{margin-bottom:12px}
section ul{padding-left:20px} section li{margin:4px 0}
.paused{margin-top:14px;color:var(--muted);font-size:14.5px;border-left:3px solid var(--rival);padding-left:12px;max-width:62ch}
.steps{list-style:none;counter-reset:s;padding:0}
.steps>li{counter-increment:s;position:relative;padding:0 0 22px 52px;margin:0}
.steps>li::before{content:counter(s);position:absolute;left:0;top:-4px;font:500 34px/1 var(--serif);color:var(--mark)}
.steps h3{font-size:17px;font-weight:700;margin-bottom:4px}
.ex{color:var(--muted);font-size:15px;border-left:2px solid var(--rule);padding-left:12px}
.ex i{color:var(--ink)}
.run{padding:16px 0;border-bottom:1px solid var(--rule)}
.run h3{font:500 20px/1.35 var(--serif);margin-bottom:6px}
.run h3 a{text-decoration:none}
.run.usable h3 a{background:linear-gradient(transparent 55%,var(--mark-bg) 55%)}
.run.thin h3 a{text-decoration:underline 2px var(--rival);text-underline-offset:5px}
.run.off_topic h3 a{color:var(--muted)}
.vd{font-size:15px;margin-bottom:6px}
.run.usable .vd b{color:var(--mark)} .run.thin .vd b{color:var(--rival)} .run.off_topic .vd b{color:var(--flag)}
.q{color:var(--muted);font:italic 400 16px/1.5 var(--serif)}
.q span{font:400 14px var(--sans);font-style:normal;color:var(--faint)}
.heads li{color:var(--muted);font:italic 400 16px/1.5 var(--serif);margin:8px 0}
.two{display:grid;grid-template-columns:1fr 1fr;gap:28px;max-width:none}
.two h2{font-size:22px}
.gloss{font-size:15.5px;gap:8px 20px}
details summary{cursor:pointer;list-style:none}
details summary::-webkit-details-marker{display:none}
details summary h2{display:inline;text-decoration:underline dotted var(--faint);text-underline-offset:6px}
details[open] summary h2{text-decoration:none}
pre{background:var(--surface);border-radius:6px;padding:14px 16px;overflow-x:auto;margin-top:8px}
pre code{background:none;padding:0;font-size:13.5px;line-height:1.7}
@media (max-width:860px){.two{grid-template-columns:1fr}.steps>li{padding-left:40px}.steps>li::before{font-size:28px}}
"""
    return HTMLResponse(theme.page("Simulations", body, css, tab_title="Simulations — IdeaPulse"))
