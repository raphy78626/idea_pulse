#!/usr/bin/env python3
"""Headless CLI for IdeaPulse — designed for terminal use and Hermes Agent integration.

Provides fast, structured (human or --json) access to:
  - status / brief      Live server health + first-dollar pipeline summary
  - pains               Verified pain posts with exact quotes & buyer signals
  - money               First-customer offers (Opportunities / Verified pains / Products)
  - outreach            Prospect pipeline, due follow-ups, and drafted messages
  - add-lead <item_id>  Idempotently stage a verified PAIN (never BUILDER) into /outreach
  - mark <pid> <stage>  Advance a prospect's pipeline stage
  - eval-money          Score pending verified pains & opportunities for first payment
  - harvest / extract   Run Stage 1 Reddit pain harvest or Stage 2 pain extraction
  - restart             Safely restart LaunchAgent com.rahil.ideapulse

Safety boundary: this CLI stages leads and drafts messages for human approval;
it never auto-sends Reddit replies, DMs, or WhatsApp messages.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import app as ip  # noqa: E402
import money as money_mod  # noqa: E402
import pain_extract  # noqa: E402
import pains_page  # noqa: E402

SERVER_URL = os.environ.get("IDEAPULSE_URL", "http://127.0.0.1:8300")
LAUNCHD_LABEL = "com.rahil.ideapulse"


def _conn():
    conn = ip.db()
    pain_extract.ensure_table(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS prospects (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT,
        name TEXT, business TEXT, phone TEXT, channel TEXT DEFAULT 'whatsapp',
        status TEXT DEFAULT 'new', template TEXT DEFAULT 'hinglish_dental',
        last_contact TEXT, followup TEXT, notes TEXT)""")
    conn.row_factory = sqlite3.Row
    return conn


def _server_health(url=SERVER_URL):
    try:
        import httpx
        r = httpx.get(url.rstrip("/") + "/", timeout=2.5)
        return {"up": r.status_code == 200, "status_code": r.status_code, "url": url}
    except Exception as e:
        return {"up": False, "status_code": 0, "url": url, "error": type(e).__name__}


def _existing_leads(conn):
    rows = conn.execute("SELECT id, name, notes FROM prospects").fetchall()
    by_name = {}
    by_url = []
    for r in rows:
        nm = (r["name"] or "").strip().lower()
        if nm:
            by_name[nm] = r["id"]
        if r["notes"]:
            by_url.append((r["id"], r["notes"]))
    return by_name, by_url


def _match_prospect_id(author, url, existing):
    by_name, by_url = existing
    a = (author or "").strip().lower()
    if a:
        if f"u/{a}" in by_name:
            return by_name[f"u/{a}"]
        if a in by_name:
            return by_name[a]
    u = (url or "").strip()
    if u:
        for pid, notes in by_url:
            if u in notes:
                return pid
    return None


def _draft_message(row):
    tname, ttext = ip.TEMPLATES.get(row["template"], list(ip.TEMPLATES.values())[0])
    notes_raw = row["notes"] or ""
    if notes_raw.startswith("Job: "):
        idea_txt = notes_raw[5:].split(" | ", 1)[0]
    elif notes_raw.startswith("Pitch: "):
        idea_txt = notes_raw.replace("Pitch: ", "", 1)
    else:
        idea_txt = "a new micro-product"
    msg = (ttext.replace("{name}", row["name"] or "there")
                .replace("{business}", row["business"] or "your business")
                .replace("{link}", ip.demo_link() or "(demo link coming)")
                .replace("{idea}", idea_txt[:90])
                .replace("{price}", "999"))
    return tname, msg


def get_status(check_server=True):
    conn = _conn()
    try:
        total_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        pains = dict(conn.execute("SELECT status, COUNT(*) FROM pains GROUP BY status").fetchall())
        earn = dict(conn.execute("SELECT COALESCE(earn_verdict,'PENDING'), COUNT(*) FROM items GROUP BY earn_verdict").fetchall())
        prospects = dict(conn.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status").fetchall())
        today = datetime.now().strftime("%Y-%m-%d")
        due_today = conn.execute(
            "SELECT COUNT(*) FROM prospects WHERE followup IS NOT NULL AND followup != '' "
            "AND followup <= ? AND status NOT IN ('won','dead')", (today,)).fetchone()[0]
    finally:
        conn.close()
    return {
        "server": _server_health() if check_server else {"up": None, "url": SERVER_URL},
        "items_total": total_items,
        "pains": {
            "PAIN": pains.get("PAIN", 0),
            "BUILDER": pains.get("BUILDER", 0),
            "NOT_PAIN": pains.get("NOT_PAIN", 0),
            "UNVERIFIED": pains.get("UNVERIFIED", 0),
            "ERROR": pains.get("ERROR", 0),
        },
        "money": {
            "GO": earn.get("GO", 0),
            "LATER": earn.get("LATER", 0),
            "NO": earn.get("NO", 0),
            "ERROR": earn.get("ERROR", 0),
        },
        "outreach": {
            "total": sum(prospects.values()),
            "by_status": {s: prospects.get(s, 0) for s in ip.STATUSES},
            "due_today": due_today,
        },
    }


def list_pains(status="pain", tool="", limit=10):
    status_map = {"pain": "PAIN", "builder": "BUILDER", "not_pain": "NOT_PAIN",
                  "unverified": "UNVERIFIED", "error": "ERROR", "all": None}
    st_val = status_map.get(status.lower(), "PAIN")
    conn = _conn()
    try:
        existing = _existing_leads(conn)
        where, params = "", []
        if st_val:
            where, params = "WHERE p.status = ?", [st_val]
        rows = conn.execute(
            "SELECT i.id, i.title, i.url, i.meta, i.author, i.earn_verdict, i.offer_money, p.* "
            "FROM pains p JOIN items i ON i.id = p.item_id " + where + " ORDER BY i.id DESC",
            params).fetchall()
    finally:
        conn.close()

    tool_q = tool.strip().lower()
    out = []
    for r in rows:
        tools = pains_page._tools(r)
        if tool_q and tool_q not in {t.lower() for t in tools}:
            continue
        pid = _match_prospect_id(r["author"], r["url"], existing)
        out.append({
            "id": r["id"],
            "status": r["status"],
            "author": f"u/{r['author']}" if r["author"] else "",
            "persona": r["persona"] or "",
            "job": r["job"] or "",
            "workaround": r["workaround"] or "",
            "tools": tools,
            "cost_signal": r["cost_signal"] or "",
            "pay_signal": r["pay_signal"] or "",
            "quote": r["quote"] or "",
            "title": r["title"] or "",
            "url": r["url"] or "",
            "earn_verdict": r["earn_verdict"] or "",
            "in_outreach": pid is not None,
            "prospect_id": pid,
        })
        if len(out) >= limit:
            break
    return out


def list_money(lane="opportunities", verdict="all", order="best", limit=10):
    rows = money_mod._rows(lane, verdict, order)[:limit]
    out = []
    for r in rows:
        keys = set(r.keys())
        out.append({
            "id": r["id"],
            "title": r["title"] or "",
            "source": r["source"] or "",
            "earn_verdict": r["earn_verdict"] or "",
            "what": r["what"] or "",
            "buyer": (r["buyer"] or (r["pain_persona"] if "pain_persona" in keys else "") or ""),
            "offer": r["offer_money"] or "",
            "first_payment_days": money_mod._days(r["first_payment"]),
            "capital": r["capital"],
            "proof": r["proof"] or "",
            "reason": r["reason"] or "",
            "quote": (r["pain_quote"] if "pain_quote" in keys else "") or "",
            "url": r["url"] or "",
        })
    return out


def list_outreach(status="all", due_only=False, limit=30):
    conn = _conn()
    try:
        if status in ip.STATUSES:
            rows = conn.execute("SELECT * FROM prospects WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM prospects ORDER BY id DESC").fetchall()
    finally:
        conn.close()

    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for r in rows:
        is_due = bool(r["followup"] and r["followup"] <= today and r["status"] not in ("won", "dead"))
        if due_only and not is_due:
            continue
        tname, msg = _draft_message(r)
        thread_m = re.search(r"https?://[^\s|)\"'>]+", r["notes"] or "")
        out.append({
            "id": r["id"],
            "status": r["status"],
            "name": r["name"] or "",
            "business": r["business"] or "",
            "phone": r["phone"] or "",
            "followup": r["followup"] or "",
            "due": is_due,
            "template": r["template"] or "",
            "template_label": tname,
            "notes": r["notes"] or "",
            "thread_url": thread_m.group(0) if thread_m else "",
            "draft_message": msg,
        })
        if len(out) >= limit:
            break
    return out


def add_lead(item_id, followup=None, extra_notes="", allow_builder=False):
    """Stage an item into `prospects` idempotently.

    Refuses BUILDER / NOT_PAIN rows by default so rivals are not pitched as
    customers; pass allow_builder=True only for intentional partner outreach.
    """
    conn = _conn()
    try:
        item = conn.execute("SELECT id, title, url, pitch, author, source FROM items WHERE id=?", (item_id,)).fetchone()
        if not item:
            return {"ok": False, "error": f"Item {item_id} not found", "code": 1}
        pain = conn.execute(
            "SELECT status, persona, job, quote FROM pains WHERE item_id=?", (item_id,)).fetchone()
        if pain and pain["status"] != "PAIN":
            if not (allow_builder and pain["status"] == "BUILDER"):
                return {
                    "ok": False,
                    "error": f"Item {item_id} has pain status {pain['status']} — refusing to stage non-PAIN as a customer lead (use --allow-builder for intentional partner outreach).",
                    "code": 2,
                }

        existing = _existing_leads(conn)
        dup_id = _match_prospect_id(item["author"], item["url"], existing)
        if dup_id is not None:
            row = conn.execute("SELECT * FROM prospects WHERE id=?", (dup_id,)).fetchone()
            _, msg = _draft_message(row)
            return {
                "ok": True,
                "already_exists": True,
                "prospect_id": dup_id,
                "name": row["name"] or "",
                "business": row["business"] or "",
                "status": row["status"],
                "draft_message": msg,
                "outreach_url": f"{SERVER_URL}/outreach",
            }

        if pain and pain["status"] == "PAIN":
            author = (item["author"] or "").strip()
            name = f"u/{author}" if author and author not in ("[deleted]", "[removed]") else (item["title"] or "")[:60]
            business = (pain["persona"] or "")[:120]
            template = "pain_reply"
            parts = []
            if pain["job"]:
                parts.append("Job: " + pain["job"])
            if pain["quote"]:
                parts.append('Quote: "' + pain["quote"] + '"')
            if item["url"]:
                parts.append("Thread: " + item["url"])
            if extra_notes:
                parts.append(extra_notes.strip())
            notes = " | ".join(parts)
        else:
            name = (item["author"] or "").strip() or (item["title"] or "")[:60]
            business = (item["title"] or "")[:80]
            template = "idea_pitch"
            base_note = item["pitch"] if item["pitch"] else f"Pitch: {(item['title'] or '')[:120]} (source: {item['url']})"
            notes = f"{base_note} | {extra_notes.strip()}" if extra_notes else base_note

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        cur = conn.execute(
            "INSERT INTO prospects (created,name,business,phone,status,template,followup,notes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now, name, business, "", "new", template, followup or None, notes))
        conn.commit()
        pid = cur.lastrowid
        row = conn.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        _, msg = _draft_message(row)
        return {
            "ok": True,
            "already_exists": False,
            "prospect_id": pid,
            "name": name,
            "business": business,
            "status": "new",
            "template": template,
            "notes": notes,
            "draft_message": msg,
            "outreach_url": f"{SERVER_URL}/outreach",
        }
    finally:
        conn.close()


def mark_prospect(prospect_id, stage):
    if stage not in ip.STATUSES:
        return {"ok": False, "error": f"Invalid stage '{stage}'. Valid: {', '.join(ip.STATUSES)}", "code": 2}
    conn = _conn()
    try:
        row = conn.execute("SELECT id, name, status FROM prospects WHERE id=?", (prospect_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Prospect {prospect_id} not found", "code": 1}
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        conn.execute("UPDATE prospects SET status=?, last_contact=? WHERE id=?", (stage, now, prospect_id))
        conn.commit()
        return {"ok": True, "prospect_id": prospect_id, "name": row["name"],
                "old_status": row["status"], "new_status": stage}
    finally:
        conn.close()


def build_review_packet(item_id):
    """Self-contained second-opinion prompt for logged-in Gemini / ChatGPT."""
    conn = _conn()
    try:
        item = conn.execute(
            "SELECT id, title, url, meta, source, author, body, earn_verdict, what, buyer, offer_money "
            "FROM items WHERE id=?", (item_id,)).fetchone()
        if not item:
            return {"ok": False, "error": f"Item {item_id} not found"}
        pain = conn.execute(
            "SELECT status, persona, job, workaround, tools, cost_signal, pay_signal, quote "
            "FROM pains WHERE item_id=?", (item_id,)).fetchone()
    finally:
        conn.close()

    lines = [
        "Act as a ruthless first-dollar sales critic for a solo software engineer (Python/FastAPI/Java/WhatsApp/AI) replying from a low-karma Reddit account.",
        "Decide GO or SKIP on reaching out today. If GO, write ONE short, genuinely helpful Reddit reply (under 75 words, plain human tone, NO prices/dollar figures, NO links, NO hype — give a real actionable fix plus a soft 'happy to script this if helpful, feel free to DM').",
        "",
        f"Item ID: {item['id']} | Source: {item['source']} | Author: u/{item['author'] or 'unknown'}",
        f"Title: {item['title']}",
        f"Thread URL: {item['url']}",
    ]
    if pain:
        lines.extend([
            f"Pain status: {pain['status']}",
            f"Persona: {pain['persona']}",
            f"Job to be done: {pain['job']}",
            f"Current workaround: {pain['workaround']}",
            f"Cost signal: {pain['cost_signal']}",
            f"Pay signal: {pain['pay_signal']}",
            f'Verified quote: "{pain["quote"]}"',
        ])
    if item["what"] or item["offer_money"]:
        lines.append(f"Current offer idea: {item['what']} ({item['offer_money']}) -> {item['buyer']}")
    if item["body"]:
        lines.append(f"\nOriginal post excerpt:\n{item['body'][:1500]}")
    lines.append(
        "\nReply with:\n"
        "1. COMMON_SENSE: is there already a free built-in 2-minute fix (e.g. Bulk Editor / CSV / native setting)? If yes, VERDICT must be SKIP.\n"
        "2. VERDICT: GO or SKIP (1-line reason)\n"
        "3. OFFER: exact concrete small deliverable + price (only if they reply/DM)\n"
        "4. MESSAGE: casual human reply (mix short + long sentences, contractions, NO prices, NO links, NO em dashes)"
    )
    return {"ok": True, "item_id": item_id, "prompt": "\n".join(lines)}


def build_brief(check_server=True):
    st = get_status(check_server=check_server)
    all_pains = list_pains(status="pain", limit=100)
    # Prioritize un-staged pains that mention pay or cost signals
    unclaimed = [p for p in all_pains if not p["in_outreach"]]
    unclaimed.sort(key=lambda p: (not bool(p["pay_signal"]), not bool(p["cost_signal"]), -p["id"]))
    top_pains = unclaimed[:3]
    top_go = list_money(lane="opportunities", verdict="go", order="quickest", limit=3)
    due = list_outreach(due_only=True, limit=5)
    return {
        "status": st,
        "top_pains": top_pains,
        "top_go_offers": top_go,
        "due_followups": due,
    }


def _fmt_brief(b):
    st = b["status"]
    srv = st["server"]
    srv_txt = f"UP ({srv['url']})" if srv.get("up") else f"DOWN ({srv.get('error', 'unreachable')})"
    out_s = st["outreach"]["by_status"]
    lines = [
        f"IdeaPulse Brief — server {srv_txt}",
        f"Pipeline: {st['outreach']['total']} prospects "
        f"(new {out_s['new']} · contacted {out_s['contacted']} · replied {out_s['replied']} "
        f"· call {out_s['call']} · won {out_s['won']}) | due today: {st['outreach']['due_today']}",
        f"Supply: {st['pains']['PAIN']} verified pains ({st['pains']['BUILDER']} builders excluded) "
        f"· {st['money']['GO']} GO offers · {st['items_total']:,} total items",
    ]
    if b["due_followups"]:
        lines.append("\nFollow-ups due today:")
        for p in b["due_followups"]:
            lines.append(f"  - #{p['id']} {p['name']} ({p['business']}) [{p['status']}] due {p['followup']}")
    if b["top_pains"]:
        lines.append("\nTop verified pains not yet in outreach:")
        for p in b["top_pains"]:
            sig = p["pay_signal"] or p["cost_signal"] or p["workaround"] or ""
            sig_txt = f" | signal: {sig[:70]}" if sig else ""
            lines.append(f"  - [id:{p['id']}] {p['author']} ({p['persona'][:55]}): {p['job'][:85]}{sig_txt}")
            if p["quote"]:
                lines.append(f'    "{p["quote"][:130]}"')
    if b["top_go_offers"]:
        lines.append("\nTop GO (sell now) offers:")
        for o in b["top_go_offers"]:
            days = f"~{o['first_payment_days']}d" if o["first_payment_days"] else "n/a"
            lines.append(f"  - [id:{o['id']}] {o['what'][:90]} -> {o['buyer'][:50]} ({o['offer'][:45]}, {days})")
    lines.append("\nNext: `~/bin/ideapulse add-lead <id>` to stage a pain lead, or open http://127.0.0.1:8300/outreach")
    return "\n".join(lines)


def _fmt_status(st):
    srv = st["server"]
    srv_txt = f"UP ({srv['url']})" if srv.get("up") else f"DOWN ({srv.get('error', 'unreachable')})"
    out_s = st["outreach"]["by_status"]
    return "\n".join([
        f"Server:   {srv_txt}",
        f"Items:    {st['items_total']:,}",
        f"Pains:    PAIN={st['pains']['PAIN']} BUILDER={st['pains']['BUILDER']} "
        f"NOT_PAIN={st['pains']['NOT_PAIN']} UNVERIFIED={st['pains']['UNVERIFIED']} ERROR={st['pains']['ERROR']}",
        f"Money:    GO={st['money']['GO']} LATER={st['money']['LATER']} NO={st['money']['NO']} ERROR={st['money']['ERROR']}",
        f"Outreach: total={st['outreach']['total']} "
        + " ".join(f"{k}={out_s[k]}" for k in ip.STATUSES)
        + f" (due_today={st['outreach']['due_today']})",
    ])


def _fmt_pains(rows):
    if not rows:
        return "No matching pains."
    lines = []
    for p in rows:
        tag = " [IN OUTREACH]" if p["in_outreach"] else ""
        lines.append(f"[id:{p['id']}] {p['status']}{tag} · {p['author']} — {p['persona']}")
        if p["job"]:
            lines.append(f"  Job:   {p['job']}")
        if p["quote"]:
            lines.append(f'  Quote: "{p["quote"]}"')
        if p["pay_signal"] or p["cost_signal"]:
            bits = [x for x in (f"pay: {p['pay_signal']}" if p["pay_signal"] else "",
                                f"cost: {p['cost_signal']}" if p["cost_signal"] else "") if x]
            lines.append("  Signal: " + " | ".join(bits))
        if p["tools"]:
            lines.append("  Tools: " + ", ".join(p["tools"]))
        lines.append(f"  URL:   {p['url']}")
    return "\n".join(lines)


def _fmt_money(rows):
    if not rows:
        return "No matching offers."
    lines = []
    for o in rows:
        v = o["earn_verdict"] or "PENDING"
        days = f" · ~{o['first_payment_days']}d" if o["first_payment_days"] else ""
        cap = f" · ${o['capital']} start" if o["capital"] is not None else ""
        lines.append(f"[id:{o['id']}] {v}{days}{cap} — {o['title'][:100]}")
        if o["what"]:
            lines.append(f"  Sell:  {o['what']}")
        if o["buyer"]:
            lines.append(f"  To:    {o['buyer']}")
        if o["offer"]:
            lines.append(f"  Price: {o['offer']}")
        if o["quote"]:
            lines.append(f'  Quote: "{o["quote"][:140]}"')
    return "\n".join(lines)


def _fmt_outreach(rows):
    if not rows:
        return "No prospects in this view."
    lines = []
    for r in rows:
        due_tag = " [DUE TODAY]" if r["due"] else (f" [follow up {r['followup']}]" if r["followup"] else "")
        who = r["name"] or r["business"] or "Unnamed"
        biz = f" ({r['business']})" if r["name"] and r["business"] else ""
        lines.append(f"#{r['id']} [{r['status'].upper()}]{due_tag} {who}{biz}")
        if r["phone"]:
            lines.append(f"  WhatsApp: {r['phone']}")
        if r["thread_url"]:
            lines.append(f"  Thread:   {r['thread_url']}")
        if r["notes"]:
            lines.append(f"  Notes:    {r['notes'][:160]}")
    return "\n".join(lines)


def _restart_service():
    uid = os.getuid()
    res = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{LAUNCHD_LABEL}"],
                         capture_output=True, text=True)
    if res.returncode != 0:
        return {"ok": False, "error": res.stderr.strip() or f"launchctl exited {res.returncode}"}
    for _ in range(10):
        time.sleep(0.5)
        h = _server_health()
        if h["up"]:
            return {"ok": True, "server": h}
    return {"ok": False, "error": "Service restarted but http://127.0.0.1:8300 did not respond within 5s"}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ideapulse", description="IdeaPulse CLI for terminal and Hermes Agent.")
    sub = ap.add_subparsers(dest="cmd")

    def add_json(p):
        p.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON")

    p_st = sub.add_parser("status", help="Server health and pipeline counts")
    add_json(p_st)
    p_st.add_argument("--no-server", action="store_true", help="Skip live HTTP health check")

    p_br = sub.add_parser("brief", help="Morning first-dollar brief (quota, top pains, top GO offers)")
    add_json(p_br)
    p_br.add_argument("--no-server", action="store_true", help="Skip live HTTP health check")

    p_pa = sub.add_parser("pains", help="List extracted pains")
    p_pa.add_argument("--status", default="pain", choices=["pain", "builder", "not_pain", "unverified", "error", "all"])
    p_pa.add_argument("--tool", default="", help="Filter by named tool")
    p_pa.add_argument("--limit", type=int, default=10)
    add_json(p_pa)

    p_mo = sub.add_parser("money", help="List first-customer offers")
    p_mo.add_argument("--lane", default="opportunities", choices=["opportunities", "pains", "products"])
    p_mo.add_argument("--verdict", default="all", choices=["all", "go", "later", "pending"])
    p_mo.add_argument("--order", default="best", choices=["best", "quickest", "cheapest", "hottest"])
    p_mo.add_argument("--limit", type=int, default=10)
    add_json(p_mo)

    p_ou = sub.add_parser("outreach", help="List prospects in outreach pipeline")
    p_ou.add_argument("--status", default="all", choices=["all"] + list(ip.STATUSES))
    p_ou.add_argument("--due", action="store_true", help="Only follow-ups due today or earlier")
    p_ou.add_argument("--limit", type=int, default=30)
    add_json(p_ou)

    p_al = sub.add_parser("add-lead", help="Stage a verified PAIN (or item) into /outreach")
    p_al.add_argument("item_id", type=int)
    p_al.add_argument("--followup", default="", help="Optional YYYY-MM-DD follow-up date")
    p_al.add_argument("--notes", default="", help="Extra notes to append")
    p_al.add_argument("--allow-builder", action="store_true", help="Allow staging a BUILDER for intentional partner outreach")
    add_json(p_al)

    p_rp = sub.add_parser("review-packet", help="Generate a self-contained critic prompt for Gemini/ChatGPT")
    p_rp.add_argument("item_id", type=int)
    add_json(p_rp)

    p_rc = sub.add_parser("reddit-check", help="Check Reddit Chrome login, daily cap, and top eligible PAIN leads")
    add_json(p_rc)

    p_rr = sub.add_parser("reddit-reply", help="Critic-gate and (with --send) post a helpful Reddit reply for a PAIN")
    p_rr.add_argument("item_id", type=int)
    p_rr.add_argument("--send", action="store_true", help="Actually post via logged-in Chrome (default: dry run)")
    p_rr.add_argument("--message", default=None, help="Optional explicit reply text (skips critic call)")
    p_rr.add_argument("--critic", default="auto", choices=["auto", "gemini", "llm"], help="Critic backend (default: auto = Gemini then LLM fallback)")
    p_rr.add_argument("--cap", type=int, default=0, help="Override daily cap (max 10)")
    add_json(p_rr)

    p_rb = sub.add_parser("reddit-run", help="Run critic-gated Reddit outreach batch over top PAIN leads")
    p_rb.add_argument("--max", type=int, default=3, dest="max_n", help="Max leads to process (default 3)")
    p_rb.add_argument("--send", action="store_true", help="Actually post GO verdicts (default: dry run)")
    p_rb.add_argument("--critic", default="auto", choices=["auto", "gemini", "llm"], help="Critic backend (default: auto)")
    p_rb.add_argument("--gap", type=float, default=30.0, help="Seconds between live posts (default 30)")
    p_rb.add_argument("--cap", type=int, default=0, help="Override daily cap (max 10)")
    add_json(p_rb)

    p_xc = sub.add_parser("x-check", help="Check X/Twitter Chrome login, daily cap, and top eligible X pain tweets")
    add_json(p_xc)

    p_xh = sub.add_parser("x-harvest", help="Harvest live pain tweets from x.com via logged-in Chrome")
    p_xh.add_argument("--query", default="", help="Optional single custom X search query")
    p_xh.add_argument("--limit", type=int, default=8, help="Max tweets per query (default 8)")
    add_json(p_xh)

    p_xr = sub.add_parser("x-reply", help="Critic-gate and (with --send) reply to a harvested X pain tweet")
    p_xr.add_argument("item_id", type=int)
    p_xr.add_argument("--send", action="store_true", help="Actually post reply on x.com (default: dry run)")
    p_xr.add_argument("--message", default=None, help="Optional explicit reply text (skips critic call)")
    p_xr.add_argument("--critic", default="auto", choices=["auto", "gemini", "llm"], help="Critic backend (default: auto)")
    p_xr.add_argument("--cap", type=int, default=0, help="Override daily cap (max 10)")
    add_json(p_xr)

    p_xb = sub.add_parser("x-run", help="Run critic-gated X/Twitter outreach batch over harvested pain tweets")
    p_xb.add_argument("--max", type=int, default=3, dest="max_n", help="Max tweets to process (default 3)")
    p_xb.add_argument("--send", action="store_true", help="Actually post GO verdicts (default: dry run)")
    p_xb.add_argument("--critic", default="auto", choices=["auto", "gemini", "llm"], help="Critic backend (default: auto)")
    p_xb.add_argument("--gap", type=float, default=30.0, help="Seconds between live replies (default 30)")
    p_xb.add_argument("--cap", type=int, default=0, help="Override daily cap (max 10)")
    add_json(p_xb)

    p_hu = sub.add_parser("humanize", help="Humanize + score any outreach draft (blader/humanizer + ig-human)")
    p_hu.add_argument("--text", default="", help="Draft text (or pipe via stdin)")
    p_hu.add_argument("--no-rewrite", action="store_true", help="Clean + score only without LLM rewrite pass")
    p_hu.add_argument("--max-chars", type=int, default=0, help="Optional max character hint (e.g. 220 for X)")
    add_json(p_hu)

    p_mk = sub.add_parser("mark", help="Update a prospect's pipeline stage")
    p_mk.add_argument("prospect_id", type=int)
    p_mk.add_argument("stage", choices=list(ip.STATUSES))
    add_json(p_mk)

    p_ev = sub.add_parser("eval-money", help="Run first-currency evaluation on pending verified pains & opportunities")
    p_ev.add_argument("--limit", type=int, default=6)
    add_json(p_ev)

    p_hv = sub.add_parser("harvest", help="Run Stage 1 Reddit pain harvest via Arctic Shift")
    add_json(p_hv)

    p_ex = sub.add_parser("extract", help="Run Stage 2 structured pain extraction")
    p_ex.add_argument("--limit", type=int, default=20)
    add_json(p_ex)

    p_rs = sub.add_parser("restart", help="Restart IdeaPulse LaunchAgent safely")
    add_json(p_rs)

    args = ap.parse_args(argv)
    if not args.cmd:
        args = ap.parse_args(["brief"])

    def emit(data, text):
        if getattr(args, "as_json", False):
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(text)

    if args.cmd == "status":
        st = get_status(check_server=not args.no_server)
        emit(st, _fmt_status(st))
        return 0
    if args.cmd == "brief":
        b = build_brief(check_server=not args.no_server)
        emit(b, _fmt_brief(b))
        return 0
    if args.cmd == "pains":
        rows = list_pains(status=args.status, tool=args.tool, limit=args.limit)
        emit(rows, _fmt_pains(rows))
        return 0
    if args.cmd == "money":
        rows = list_money(lane=args.lane, verdict=args.verdict, order=args.order, limit=args.limit)
        emit(rows, _fmt_money(rows))
        return 0
    if args.cmd == "outreach":
        rows = list_outreach(status=args.status, due_only=args.due, limit=args.limit)
        emit(rows, _fmt_outreach(rows))
        return 0
    if args.cmd == "add-lead":
        res = add_lead(args.item_id, followup=args.followup or None, extra_notes=args.notes,
                       allow_builder=args.allow_builder)
        if not res["ok"]:
            emit(res, f"ERROR: {res['error']}")
            return res.get("code", 1)
        verb = "Already in outreach" if res["already_exists"] else "Staged lead"
        txt = (f"{verb}: #{res['prospect_id']} {res['name']} ({res['business']}) [{res['status']}]\n"
               f"Review & send at: {res['outreach_url']}\n\nDraft message:\n{res['draft_message']}")
        emit(res, txt)
        return 0
    if args.cmd == "review-packet":
        res = build_review_packet(args.item_id)
        if not res["ok"]:
            emit(res, f"ERROR: {res['error']}")
            return 1
        emit(res, res["prompt"])
        return 0
    if args.cmd == "reddit-check":
        import reddit_outreach as ro
        login = ro.check_reddit_login()
        conn = ro._conn()
        try:
            used = ro.sent_today(conn, "reddit")
        finally:
            conn.close()
        cands = ro.candidate_pains(limit=5)
        res = {"login": login, "sent_today": used, "daily_cap": ro.DAILY_CAP, "candidates": cands}
        who = f"u/{login['user']}" if login.get("logged_in") else f"NOT LOGGED IN ({login.get('error', '')})"
        lines = [
            f"Reddit Chrome: {who}",
            f"Daily cap:     {used} / {ro.DAILY_CAP} sent today",
            "Top eligible verified pains:",
        ]
        for c in cands:
            sig = c["pay_signal"] or c["cost_signal"] or ""
            lines.append(f"  - [id:{c['id']}] {c['author']} — {c['job'][:80]}" + (f" ({sig[:50]})" if sig else ""))
        emit(res, "\n".join(lines))
        return 0 if login.get("logged_in") else 2
    if args.cmd == "reddit-reply":
        import reddit_outreach as ro
        cap = min(10, max(1, args.cap)) if args.cap else ro.DAILY_CAP
        res = ro.reply_to_pain(args.item_id, send=args.send, message=args.message, critic=args.critic, daily_cap=cap)
        if not res.get("ok"):
            emit(res, f"ERROR: {res.get('error')}")
            return res.get("code", 1)
        act = res["action"]
        if act == "skipped":
            cs = f"\nCommon sense: {res['common_sense']}" if res.get("common_sense") else ""
            txt = f"SKIP [id:{res['item_id']}] {res['author']} — {res['reason']}{cs}"
        elif act == "dry_run":
            cs = f"Common:  {res['common_sense']}\n" if res.get("common_sense") else ""
            hs = f"Human:   {res.get('human_score', 0):.1f} {res.get('human_verdict', '')}" + (
                f" (weakest: {res['human_weakest']})" if res.get("human_verdict") != "PASS" and res.get("human_weakest") else ""
            )
            txt = (f"DRY RUN [id:{res['item_id']}] {res['author']} ({res['critic_model']} -> {res['verdict']})\n"
                   f"Thread:  {res['thread_url']}\n"
                   f"{cs}"
                   f"Offer:   {res['offer']}\n"
                   f"{hs}\n"
                   f"Message:\n{res['message']}\n\n"
                   f"Re-run with --send to post ({res['sent_today']}/{res['daily_cap']} sent today).")
        else:
            txt = (f"SENT [id:{res['item_id']}] to {res['author']} as u/{res.get('posted_as', '')}\n"
                   f"Posted:   {res.get('posted_url')}\n"
                   f"Prospect: #{res.get('prospect_id')} [contacted] · follow up {res.get('followup')}\n"
                   f"Daily:    {res['sent_today']}/{res['daily_cap']} sent today")
        emit(res, txt)
        return 0
    if args.cmd == "reddit-run":
        import reddit_outreach as ro
        cap = min(10, max(1, args.cap)) if args.cap else ro.DAILY_CAP
        res = ro.run_batch(limit=args.max_n, send=args.send, critic=args.critic, daily_cap=cap, gap_s=args.gap)
        if not res.get("ok"):
            emit(res, f"ERROR: {res.get('error')}")
            return 2
        lines = [f"Reddit outreach batch ({res['mode']}) — sent_now={res['sent_now']} · today={res['sent_today']}/{res['daily_cap']}"]
        for r in res["results"]:
            if not r.get("ok"):
                lines.append(f"  - ERROR: {r.get('error')}")
            else:
                lines.append(f"  - [{r['action'].upper()}] id:{r['item_id']} {r['author']} — {r.get('reason', '')[:80]}")
                if r.get("message"):
                    lines.append(f"    msg: {r['message'][:140]}")
        emit(res, "\n".join(lines))
        return 0
    if args.cmd == "x-check":
        import reddit_outreach as ro
        import x_outreach as xo
        login = xo.check_x_login()
        conn = ro._conn()
        try:
            used = ro.sent_today(conn, "x")
        finally:
            conn.close()
        cands = xo.candidate_x_items(limit=5)
        res = {"login": login, "sent_today": used, "daily_cap": xo.DAILY_CAP, "candidates": cands}
        who = login.get("account") or login.get("handle") or "logged in" if login.get("logged_in") else f"NOT LOGGED IN ({login.get('error', '')})"
        lines = [
            f"X Chrome:  {who}",
            f"Daily cap: {used} / {xo.DAILY_CAP} sent today",
            "Top eligible X pain tweets:",
        ]
        for c in cands:
            lines.append(f"  - [id:{c['id']}] {c['author']} — {c['text'][:100]}")
        if not cands:
            lines.append("  (none yet — run `~/bin/ideapulse x-harvest`)")
        emit(res, "\n".join(lines))
        return 0 if login.get("logged_in") else 2
    if args.cmd == "x-harvest":
        import x_outreach as xo
        qs = [args.query] if args.query else None
        res = xo.harvest_x_pains(queries=qs, per_query=args.limit)
        lines = [f"Harvested {res['fetched']} X pain tweets ({res['saved_new']} new saved)."]
        for s in res["sample"]:
            lines.append(f"  - {s}")
        emit(res, "\n".join(lines))
        return 0
    if args.cmd == "x-reply":
        import x_outreach as xo
        cap = min(10, max(1, args.cap)) if args.cap else xo.DAILY_CAP
        res = xo.reply_to_tweet(args.item_id, send=args.send, message=args.message, critic=args.critic, daily_cap=cap)
        if not res.get("ok"):
            emit(res, f"ERROR: {res.get('error')}")
            return res.get("code", 1)
        act = res["action"]
        if act == "skipped":
            cs = f"\nCommon sense: {res['common_sense']}" if res.get("common_sense") else ""
            txt = f"SKIP [id:{res['item_id']}] {res['author']} — {res['reason']}{cs}"
        elif act == "dry_run":
            cs = f"Common:  {res['common_sense']}\n" if res.get("common_sense") else ""
            hs = f"Human:   {res.get('human_score', 0):.1f} {res.get('human_verdict', '')}" + (
                f" (weakest: {res['human_weakest']})" if res.get("human_verdict") != "PASS" and res.get("human_weakest") else ""
            )
            txt = (f"DRY RUN [id:{res['item_id']}] {res['author']} ({res['critic_model']} -> {res['verdict']})\n"
                   f"Tweet:   {res['thread_url']}\n"
                   f"{cs}"
                   f"Offer:   {res['offer']}\n"
                   f"{hs}\n"
                   f"Reply ({len(res['message'])} chars):\n{res['message']}\n\n"
                   f"Re-run with --send to post ({res['sent_today']}/{res['daily_cap']} sent today).")
        else:
            txt = (f"SENT [id:{res['item_id']}] to {res['author']} as {res.get('posted_as', '')}\n"
                   f"Tweet:    {res.get('posted_url')}\n"
                   f"Prospect: #{res.get('prospect_id')} [contacted] · follow up {res.get('followup')}\n"
                   f"Daily:    {res['sent_today']}/{res['daily_cap']} sent today")
        emit(res, txt)
        return 0
    if args.cmd == "x-run":
        import x_outreach as xo
        cap = min(10, max(1, args.cap)) if args.cap else xo.DAILY_CAP
        res = xo.run_x_batch(limit=args.max_n, send=args.send, critic=args.critic, daily_cap=cap, gap_s=args.gap)
        if not res.get("ok"):
            emit(res, f"ERROR: {res.get('error')}")
            return 2
        lines = [f"X outreach batch ({res['mode']}) — sent_now={res['sent_now']} · today={res['sent_today']}/{res['daily_cap']}"]
        for r in res["results"]:
            if not r.get("ok"):
                lines.append(f"  - ERROR: {r.get('error')}")
            else:
                lines.append(f"  - [{r['action'].upper()}] id:{r['item_id']} {r['author']} — {r.get('reason', '')[:80]}")
                if r.get("message"):
                    lines.append(f"    reply: {r['message']}")
        emit(res, "\n".join(lines))
        return 0
    if args.cmd == "humanize":
        import humanizer
        raw = args.text if args.text else sys.stdin.read()
        if not raw.strip():
            emit({"ok": False, "error": "no text provided (use --text or stdin)"}, "ERROR: no text provided")
            return 1
        rw = None if args.no_rewrite else humanizer._default_rewrite
        res = humanizer.humanize_reply(raw, max_chars=(args.max_chars or None), rewrite_fn=rw)
        res["ok"] = True
        res["blader_skill"] = str(humanizer.blader_skill_path() or "")
        txt = (f"Human score: {res['human_score']:.1f} {res['verdict']}"
               + (f" (weakest: {res['weakest']})" if res["verdict"] != "PASS" and res["weakest"] else "")
               + f" · rewrites={res['rewrites']} · artefacts_stripped={res['changes']}\n\n{res['text']}")
        emit(res, txt)
        return 0 if res["verdict"] == "PASS" else 2
    if args.cmd == "mark":
        res = mark_prospect(args.prospect_id, args.stage)
        if not res["ok"]:
            emit(res, f"ERROR: {res['error']}")
            return res.get("code", 1)
        emit(res, f"Prospect #{res['prospect_id']} ({res['name']}): {res['old_status']} -> {res['new_status']}")
        return 0
    if args.cmd == "eval-money":
        res = money_mod.money_evaluate(limit=args.limit, async_=0)
        emit(res, f"Evaluated {res['evaluated']} opportunities · models: {res['models']}")
        return 0
    if args.cmd == "harvest":
        import httpx
        import pain_harvest
        with httpx.Client(headers=ip.UA, timeout=40, follow_redirects=True) as client:
            got = pain_harvest.fetch(client)
        n = ip.save_items("Reddit pains", got)
        ip.set_status("Reddit pains", "ok", n)
        res = {"fetched": len(got), "saved_new": n}
        emit(res, f"Harvested {len(got)} pain posts ({n} new saved).")
        return 0
    if args.cmd == "extract":
        conn = _conn()
        try:
            summary = pain_extract.run(conn, limit=args.limit)
            left = len(pain_extract.pending(conn))
        finally:
            conn.close()
        res = {"summary": summary, "remaining": left}
        emit(res, f"Extracted {summary['done']} posts: PAIN={summary['PAIN']} BUILDER={summary['BUILDER']} "
                  f"NOT_PAIN={summary['NOT_PAIN']} UNVERIFIED={summary['UNVERIFIED']} ERROR={summary['ERROR']} "
                  f"(remaining: {left})")
        return 0
    if args.cmd == "restart":
        res = _restart_service()
        if not res["ok"]:
            emit(res, f"ERROR: {res['error']}")
            return 1
        emit(res, f"IdeaPulse restarted and healthy -> {res['server']['url']}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
