"""Critic-gated, anti-ban Reddit outreach for IdeaPulse verified PAIN leads.

Workflow:
  1. Verify item is a `PAIN` (never `BUILDER` / `NOT_PAIN`) and not already contacted.
  2. Enforce hard daily cap (`DAILY_CAP`, default 5, max 10) via `outreach_log`.
  3. Build the lead's review packet and consult already-logged-in Gemini (`~/bin/gemini-ask`)
     as a ruthless GO/SKIP critic + human message writer (or accept an explicit --message).
  4. Validate message hygiene (length, no leaked labels, no AI slop, no spam links).
  5. Dry-run by default; with `send=True`, post on `old.reddit.com` via `browser-harness`
     (checking login, archived/locked state, and post-submit rate-limit errors), record in
     `outreach_log`, and mark the prospect `contacted` in `prospects` with a 3-day follow-up.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import app as ip
import ideapulse_cli as cli
import pain_extract

DAILY_CAP = min(10, max(1, int(os.environ.get("IDEAPULSE_REDDIT_DAILY_CAP", "5"))))
GEMINI_BIN = Path.home() / "bin" / "gemini-ask"

_BAD_MARKERS = (
    "verdict:",
    "offer:",
    "message:",
    "here is a reply",
    "here's a reply",
    "as an ai",
    "<think>",
    "trycloudflare.com",
    "bit.ly/",
    "tinyurl.com/",
)


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS outreach_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER,
        prospect_id INTEGER,
        channel TEXT DEFAULT 'reddit',
        thread_url TEXT,
        author TEXT,
        message TEXT,
        critic_model TEXT,
        critic_verdict TEXT,
        posted_url TEXT,
        status TEXT,
        error TEXT,
        created_at TEXT
    )""")


def _conn():
    conn = cli._conn()
    ensure_table(conn)
    return conn


def to_old_reddit(url):
    url = (url or "").strip()
    if not url:
        return ""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if "reddit.com" not in host:
        return url
    return p._replace(scheme="https", netloc="old.reddit.com").geturl()


def sent_today(conn, channel="reddit"):
    ensure_table(conn)
    day = datetime.now().strftime("%Y-%m-%d")
    return conn.execute(
        "SELECT COUNT(*) FROM outreach_log WHERE channel=? AND status='sent' AND created_at LIKE ?",
        (channel, day + "%"),
    ).fetchone()[0]


def already_contacted(conn, item_id, author, url):
    ensure_table(conn)
    row = conn.execute(
        "SELECT id, status FROM outreach_log WHERE status='sent' AND (item_id=? OR thread_url=? OR (author!='' AND lower(author)=lower(?))) LIMIT 1",
        (item_id, url or "", (author or "").strip()),
    ).fetchone()
    if row:
        return f"already sent in outreach_log #{row['id']}"
    existing = cli._existing_leads(conn)
    pid = cli._match_prospect_id(author, url, existing)
    if pid is not None:
        p = conn.execute("SELECT id, status FROM prospects WHERE id=?", (pid,)).fetchone()
        if p and p["status"] != "new":
            return f"prospect #{pid} is already '{p['status']}'"
    return ""


def parse_critic_reply(text):
    """Extract verdict (GO|SKIP), reason, offer, and message from critic reply.

    Sets `parsed=False` when no explicit VERDICT: GO|SKIP line is present so
    broken/empty critic output (e.g. a bare 'Gemini said' stub) is treated as
    an error/fallback rather than silently buried as SKIP.
    """
    raw = (text or "").strip()
    if not raw:
        return {"parsed": False, "verdict": "SKIP", "reason": "empty critic reply", "offer": "", "message": ""}

    # Normalize markdown bold/numbered prefixes on labels: "1. **VERDICT:** GO"
    norm = re.sub(r"(?m)^\s*(?:\d+[\.\)]\s*)?\*{0,2}(VERDICT|OFFER|MESSAGE)\*{0,2}\s*:\s*\*{0,2}", r"\1: ", raw, flags=re.I)

    vm = re.search(r"(?im)^VERDICT:\s*(GO|SKIP)\b(.*)$", norm)
    if not vm:
        return {"parsed": False, "verdict": "SKIP", "reason": "could not parse VERDICT line", "offer": "", "message": ""}
    verdict = vm.group(1).upper()
    reason = vm.group(2).strip(" -—:.*")

    om = re.search(r"(?ims)^OFFER:\s*(.+?)(?=^(?:MESSAGE|VERDICT):|\Z)", norm)
    offer = om.group(1).strip() if om else ""

    mm = re.search(r"(?ims)^MESSAGE:\s*(.+?)(?=^(?:VERDICT|OFFER|NOTES?):|\Z)", norm)
    message = mm.group(1).strip() if mm else ""
    # Strip wrapping quotes or code fences around MESSAGE
    message = re.sub(r"^```[a-z]*\s*|\s*```$", "", message, flags=re.I).strip()
    if len(message) >= 2 and message[0] == message[-1] and message[0] in ('"', "'"):
        message = message[1:-1].strip()

    return {"parsed": True, "verdict": verdict, "reason": reason, "offer": offer, "message": message}


def validate_reply(text):
    """Return (ok, error_reason). Guards against spam, leaks, and empty replies."""
    msg = (text or "").strip()
    if len(msg) < 15:
        return False, "message too short (<15 chars)"
    if len(msg) > 1200:
        return False, "message too long (>1200 chars)"
    low = msg.lower()
    for bad in _BAD_MARKERS:
        if bad in low:
            return False, f"message contains disallowed marker: {bad!r}"
    urls = re.findall(r"https?://\S+", msg)
    if len(urls) > 1:
        return False, f"too many links ({len(urls)}); max 1 allowed"
    return True, ""


CRITIC_SYSTEM = (
    "You are a ruthless first-dollar sales critic for a solo software engineer "
    "(Python, FastAPI, Java, WhatsApp bots, AI/LLM automation). "
    "Evaluate one Reddit buyer post and reply in this EXACT 3-field format:\n"
    "VERDICT: GO or SKIP — one-line reason\n"
    "OFFER: concrete small done-for-you deliverable + price (or 'none' if SKIP)\n"
    "MESSAGE: ONE short, genuinely helpful, non-spammy Reddit reply (under 80 words, "
    "plain human tone, no hype, no link dumps, directly addresses their exact pain; or 'none' if SKIP)"
)


def _ask_gemini(prompt, timeout=60):
    if not GEMINI_BIN.exists():
        return "", "gemini", f"{GEMINI_BIN} not found"
    try:
        res = subprocess.run(
            [str(GEMINI_BIN), "--timeout", str(timeout), "-"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout + 25,
        )
    except subprocess.TimeoutExpired:
        return "", "gemini", f"gemini-ask timed out after {timeout}s"

    out = res.stdout or ""
    m = re.search(r"---REPLY---\n(.*?)\n---END---", out, re.S)
    reply = m.group(1).strip() if m else ""
    if res.returncode != 0 or not reply or not parse_critic_reply(reply)["parsed"]:
        err = (res.stderr or reply or f"gemini-ask exited {res.returncode}").strip()
        return "", "gemini", f"unusable gemini reply: {err[:200]}"
    return reply, "gemini-chrome", ""


def _ask_llm(prompt, timeout=90):
    import llm
    text, model = llm.chat(CRITIC_SYSTEM, prompt, max_tokens=1200, timeout=timeout, want_json=False)
    if not text or not parse_critic_reply(text)["parsed"]:
        return "", model or "llm", f"unusable llm reply ({model}): {(text or '')[:160]}"
    return text, model, ""


def ask_critic(prompt, critic="auto", timeout=60):
    """Consult critic (`auto`, `gemini`, or `llm`) and return (reply_text, model_name, error).

    `auto` tries logged-in Gemini (`~/bin/gemini-ask`, 60s cap) first and falls
    back cleanly to free OpenRouter + local Ollama (`llm.chat`) if Gemini stalls
    or returns an unparseable stub.
    """
    if critic == "llm":
        return _ask_llm(prompt, timeout=max(timeout, 90))
    if critic == "gemini":
        return _ask_gemini(prompt, timeout=timeout)
    if critic == "auto":
        reply, model, err = _ask_gemini(prompt, timeout=timeout)
        if not err:
            return reply, model, ""
        reply2, model2, err2 = _ask_llm(prompt, timeout=90)
        if not err2:
            return reply2, model2, ""
        return "", "auto", f"gemini failed ({err}); llm fallback failed ({err2})"
    return "", critic, f"unsupported critic '{critic}' (supported: auto, gemini, llm)"


def _run_harness(script, timeout=45):
    """Execute a browser-harness script, auto-recycling a stale daemon once on keepalive timeout."""
    harness = shutil.which("browser-harness")
    if not harness:
        return False, "", "browser-harness not found on PATH"
    for attempt in range(2):
        try:
            res = subprocess.run([harness], input=script, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "", f"browser-harness timed out after {timeout}s"
        err = (res.stderr or "").strip()
        if res.returncode == 0:
            return True, res.stdout or "", ""
        if attempt == 0 and "keepalive ping timeout" in err.lower():
            subprocess.run(["pkill", "-f", "browser-harness/.*daemon.py"], capture_output=True)
            time.sleep(1.0)
            continue
        return False, res.stdout or "", err[-400:] or f"browser-harness exited {res.returncode}"
    return False, "", "browser-harness failed after retry"


def check_reddit_login():
    """Verify Chrome is logged in to Reddit without modifying anything."""
    script = r"""
import json
tid = new_tab("https://old.reddit.com/api/me.json")
wait_for_load()
wait(1.5)
r = cdp("Runtime.evaluate", expression="document.body ? document.body.innerText : ''", returnByValue=True)
txt = r.get("result", {}).get("value", "")
try:
    cdp("Target.closeTarget", targetId=tid)
except Exception:
    pass
try:
    d = json.loads(txt)
    data = d.get("data") or {}
    out = {"logged_in": bool(data.get("name")), "user": data.get("name", ""), "suspended": bool(data.get("is_suspended"))}
except Exception as e:
    out = {"logged_in": False, "user": "", "error": str(e)}
print("REDDIT_ME:" + json.dumps(out))
"""
    ok, stdout, err = _run_harness(script, timeout=35)
    if not ok:
        return {"logged_in": False, "user": "", "error": err}
    m = re.search(r"REDDIT_ME:(\{.*\})", stdout)
    if not m:
        return {"logged_in": False, "user": "", "error": "no response from old.reddit.com/api/me.json"}
    return json.loads(m.group(1))


def post_reddit_comment(thread_url, message):
    """Post a top-level comment on an old.reddit.com thread via logged-in Chrome."""
    old_url = to_old_reddit(thread_url)
    payload = json.dumps({"url": old_url, "message": message})
    script = f"JOB = {payload}\n" + r"""
import json, time
tid = new_tab(JOB["url"])
wait_for_load()
wait(2.0)

def ev(expr):
    r = cdp("Runtime.evaluate", expression=expr, returnByValue=True)
    return r.get("result", {}).get("value")

state = json.loads(ev('''JSON.stringify({
  loggedIn: !!document.querySelector('.user a'),
  user: document.querySelector('.user a') ? document.querySelector('.user a').textContent : '',
  archived: !!document.querySelector('.archived-infobar'),
  locked: !!document.querySelector('.locked-infobar'),
  hasTextarea: !!document.querySelector('form.usertext.cloneable textarea[name="text"]'),
  hasSaveBtn: !!document.querySelector('form.usertext.cloneable button.save[type="submit"]')
})'''))

result = {"ok": False, "user": state.get("user", ""), "url": JOB["url"]}
if not state.get("loggedIn"):
    result["error"] = "Not logged in to Reddit in Chrome"
elif state.get("archived"):
    result["error"] = "Thread is archived"
elif state.get("locked"):
    result["error"] = "Thread is locked"
elif not (state.get("hasTextarea") and state.get("hasSaveBtn")):
    result["error"] = "Comment form not found on thread"
else:
    msg_js = json.dumps(JOB["message"])
    ev(f'''(() => {{
      const ta = document.querySelector('form.usertext.cloneable textarea[name="text"]');
      ta.focus();
      ta.value = {msg_js};
      ta.dispatchEvent(new Event('input', {{bubbles: true}}));
      ta.dispatchEvent(new Event('change', {{bubbles: true}}));
      document.querySelector('form.usertext.cloneable button.save[type="submit"]').click();
    }})()''')
    wait(3.5)
    after = json.loads(ev('''JSON.stringify({
      err: (document.querySelector('form.usertext.cloneable .status.error')?.innerText || '').trim(),
      status: (document.querySelector('form.usertext.cloneable .status')?.innerText || '').trim(),
      taVal: (document.querySelector('form.usertext.cloneable textarea[name="text"]')?.value || '').trim(),
      permalink: (document.querySelector('.commentarea .sitetable .comment a.bylink')?.href || '')
    })'''))
    if after.get("err"):
        result["error"] = "Reddit error: " + after["err"]
    elif after.get("taVal") == "":
        result["ok"] = True
        result["posted_url"] = after.get("permalink") or JOB["url"]
    else:
        result["error"] = "Submit did not clear textarea (status: " + (after.get("status") or "unknown") + ")"

try:
    cdp("Target.closeTarget", targetId=tid)
except Exception:
    pass
print("REDDIT_POST:" + json.dumps(result))
"""
    ok, stdout, err = _run_harness(script, timeout=50)
    if not ok:
        return {"ok": False, "error": err}
    m = re.search(r"REDDIT_POST:(\{.*\})", stdout)
    if not m:
        return {"ok": False, "error": "no post result returned by browser-harness"}
    return json.loads(m.group(1))


def reply_to_pain(
    item_id,
    send=False,
    message=None,
    critic="auto",
    daily_cap=DAILY_CAP,
    critic_fn=ask_critic,
    post_fn=post_reddit_comment,
):
    """Evaluate and (when send=True) post a single helpful Reddit reply for a verified PAIN."""
    conn = _conn()
    try:
        item = conn.execute(
            "SELECT id, title, url, author, source FROM items WHERE id=?", (item_id,)
        ).fetchone()
        if not item:
            return {"ok": False, "error": f"Item {item_id} not found", "code": 1}
        pain = conn.execute(
            "SELECT status, persona, job, quote FROM pains WHERE item_id=?", (item_id,)
        ).fetchone()
        if not pain or pain["status"] != "PAIN":
            st = pain["status"] if pain else "NONE"
            return {"ok": False, "error": f"Item {item_id} is not a verified PAIN (status={st})", "code": 2}

        dup = already_contacted(conn, item_id, item["author"], item["url"])
        if dup:
            return {"ok": False, "error": f"Duplicate guard: {dup}", "code": 2}

        used_today = sent_today(conn, "reddit")
        if send and used_today >= daily_cap:
            return {
                "ok": False,
                "error": f"Daily Reddit cap reached ({used_today}/{daily_cap}). Try again tomorrow.",
                "code": 2,
            }
    finally:
        conn.close()

    # Critic gate (unless explicit message override provided)
    if message is not None:
        parsed = {"verdict": "GO", "reason": "manual message override", "offer": "", "message": message.strip()}
        critic_model = "manual"
    else:
        pkt = cli.build_review_packet(item_id)
        if not pkt.get("ok"):
            return {"ok": False, "error": pkt.get("error", "failed to build review packet"), "code": 1}
        raw_reply, critic_model, err = critic_fn(pkt["prompt"], critic=critic)
        if err:
            return {"ok": False, "error": f"Critic ({critic}) failed: {err}", "code": 3}
        parsed = parse_critic_reply(raw_reply)
        if not parsed["parsed"]:
            return {"ok": False, "error": f"Critic ({critic_model}) returned unparseable output (never silently skipped)", "code": 3}
        if parsed["verdict"] != "GO":
            return {
                "ok": True,
                "action": "skipped",
                "item_id": item_id,
                "author": f"u/{item['author']}" if item["author"] else "",
                "thread_url": to_old_reddit(item["url"]),
                "critic_model": critic_model,
                "verdict": parsed["verdict"],
                "reason": parsed["reason"],
            }

    valid, verr = validate_reply(parsed["message"])
    if not valid:
        return {"ok": False, "error": f"Message failed hygiene check: {verr}", "code": 2,
                "draft": parsed["message"]}

    out = {
        "ok": True,
        "action": "dry_run",
        "item_id": item_id,
        "author": f"u/{item['author']}" if item["author"] else "",
        "thread_url": to_old_reddit(item["url"]),
        "critic_model": critic_model,
        "verdict": parsed["verdict"],
        "reason": parsed["reason"],
        "offer": parsed["offer"],
        "message": parsed["message"],
        "sent_today": used_today,
        "daily_cap": daily_cap,
    }

    if not send:
        return out

    post_res = post_fn(item["url"], parsed["message"])
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not post_res.get("ok"):
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO outreach_log (item_id, prospect_id, channel, thread_url, author, message, "
                "critic_model, critic_verdict, posted_url, status, error, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, None, "reddit", item["url"], item["author"] or "", parsed["message"],
                 critic_model, parsed["verdict"], "", "error", post_res.get("error", "post failed"), now),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": False, "error": post_res.get("error", "Reddit post failed"), "code": 4,
                "thread_url": to_old_reddit(item["url"])}

    # Stage prospect & immediately mark contacted with 3-day follow-up
    fu = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    staged = cli.add_lead(item_id, followup=fu, extra_notes=f"Sent via Reddit ({critic_model})")
    pid = staged.get("prospect_id") if staged.get("ok") else None
    if pid is not None:
        cli.mark_prospect(pid, "contacted")

    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO outreach_log (item_id, prospect_id, channel, thread_url, author, message, "
            "critic_model, critic_verdict, posted_url, status, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, pid, "reddit", item["url"], item["author"] or "", parsed["message"],
             critic_model, parsed["verdict"], post_res.get("posted_url", item["url"]), "sent", "", now),
        )
        conn.commit()
    finally:
        conn.close()

    out.update({
        "action": "sent",
        "prospect_id": pid,
        "posted_url": post_res.get("posted_url", to_old_reddit(item["url"])),
        "posted_as": post_res.get("user", ""),
        "followup": fu,
        "sent_today": used_today + 1,
    })
    return out


def candidate_pains(limit=10):
    """Uncontacted verified PAIN items prioritized by explicit pay/cost signals."""
    conn = _conn()
    try:
        pains = cli.list_pains(status="pain", limit=200)
        eligible = []
        for p in pains:
            raw_author = p["author"][2:] if p["author"].startswith("u/") else p["author"]
            if already_contacted(conn, p["id"], raw_author, p["url"]):
                continue
            eligible.append(p)
    finally:
        conn.close()
    eligible.sort(key=lambda p: (not bool(p["pay_signal"]), not bool(p["cost_signal"]), -p["id"]))
    return eligible[:limit]


def run_batch(
    limit=3,
    send=False,
    critic="auto",
    daily_cap=DAILY_CAP,
    gap_s=30.0,
    critic_fn=ask_critic,
    post_fn=post_reddit_comment,
    sleep_fn=time.sleep,
):
    """Run critic-gated Reddit outreach over top candidate pains up to limit / remaining daily cap."""
    conn = _conn()
    try:
        used = sent_today(conn, "reddit")
    finally:
        conn.close()

    remaining = max(0, daily_cap - used)
    if send and remaining == 0:
        return {
            "ok": False,
            "error": f"Daily Reddit cap already reached ({used}/{daily_cap}).",
            "sent_today": used,
            "daily_cap": daily_cap,
            "results": [],
        }

    target = min(limit, remaining) if send else limit
    candidates = candidate_pains(limit=max(target * 2, 6))
    results = []
    sent_count = 0

    for cand in candidates:
        if send and sent_count >= target:
            break
        if not send and len(results) >= target:
            break
        r = reply_to_pain(
            cand["id"],
            send=send,
            critic=critic,
            daily_cap=daily_cap,
            critic_fn=critic_fn,
            post_fn=post_fn,
        )
        results.append(r)
        if not r.get("ok"):
            # Halt immediately on platform/rate-limit error when sending
            if send and r.get("code") == 4:
                break
            continue
        if r.get("action") == "sent":
            sent_count += 1
            if sent_count < target and gap_s > 0:
                sleep_fn(gap_s)

    return {
        "ok": True,
        "mode": "send" if send else "dry_run",
        "sent_now": sent_count,
        "sent_today": used + sent_count,
        "daily_cap": daily_cap,
        "results": results,
    }
