"""Critic-gated, anti-ban X (Twitter) pain harvesting & outreach via logged-in Chrome.

Uses the user's already-logged-in Chrome X session (`browser-harness`) to:
  1. Harvest fresh live pain tweets (`x-harvest`) into IdeaPulse `items` (`source='X pains'`,
     `kind='pain'`), deduplicated by status URL.
  2. Critic-gate each tweet (`auto` = Gemini Chrome -> OpenRouter/Ollama fallback, or `--critic llm`)
     producing a concise (<240 chars), genuinely helpful, non-spammy reply with NO links, NO
     hashtags, and NO cold price tags.
  3. Enforce hard anti-ban rails: daily cap (`DAILY_CAP`, default 5, max 10) persisted in
     `outreach_log` (`channel='x'`), zero duplicates by item_id / thread_url / @handle,
     refusal of BUILDER / NOT_PAIN rows, and immediate halt on any X rate-limit/error toast.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import app as ip
import ideapulse_cli as cli
import reddit_outreach as ro

DAILY_CAP = min(10, max(1, int(os.environ.get("IDEAPULSE_X_DAILY_CAP", "5"))))
MAX_REPLY_CHARS = 260
SOURCE_NAME = "X pains"

DEFAULT_QUERIES = [
    '("is there a tool" OR "is there an app" OR "anyone know a tool") (shopify OR stripe OR quickbooks OR notion OR slack OR excel OR zapier OR invoice OR crm) ? -filter:retweets -filter:links lang:en',
    '("hate doing this manually" OR "spend hours manually" OR "doing this by hand") (spreadsheet OR csv OR invoice OR client OR report OR data) -filter:retweets -filter:links lang:en',
    '("looking for a" OR "alternative to") (quickbooks OR calendly OR hubspot OR zapier OR airtable OR mailchimp) ("too expensive" OR "pricing" OR "hate") -filter:retweets -filter:links lang:en',
]

X_CRITIC_SYSTEM = (
    "You are a ruthless, common-sense first-dollar sales critic for a solo software engineer "
    "(Python, FastAPI, Java, WhatsApp bots, AI/LLM automation) replying on X (Twitter).\n"
    "THINK TWICE — COMMON-SENSE CHECK FIRST:\n"
    "- Does the tool already have a trivial free built-in 2-minute fix? Or is this a meme/news/promo/vent with no real buyer intent? "
    "If yes, you MUST reply VERDICT: SKIP.\n"
    "RULES FOR MESSAGE:\n"
    "- MUST be under 220 characters (fits cleanly in one X reply).\n"
    "- Sound like a real engineer typing casually: mix a very short sentence (3-6 words) with one concrete sentence, use contractions (I'd, don't, it's).\n"
    "- NO links, NO hashtags, NO prices ($/₹), NO em dashes (—), NO corporate hype or 'feel free to DM' bot sign-offs.\n"
    "Reply in this EXACT 4-field format:\n"
    "COMMON_SENSE: 1-line check — is custom help actually needed or is there a trivial free built-in fix / no buyer intent?\n"
    "VERDICT: GO or SKIP — one-line reason\n"
    "OFFER: concrete small done-for-you deliverable + price to propose only if they reply/DM (or 'none' if SKIP)\n"
    "MESSAGE: the exact casual under-220-char reply (or 'none' if SKIP)"
)

_BAD_X_MARKERS = (
    "verdict:",
    "offer:",
    "message:",
    "here is a reply",
    "here's a reply",
    "as an ai",
    "<think>",
    "trycloudflare.com",
    "bit.ly/",
)


def validate_x_reply(text):
    """Return (ok, error_reason). Keeps X replies crisp, human, and anti-spam safe."""
    msg = (text or "").strip()
    if len(msg) < 15:
        return False, "reply too short (<15 chars)"
    if len(msg) > MAX_REPLY_CHARS:
        return False, f"reply too long ({len(msg)} chars; max {MAX_REPLY_CHARS} for X)"
    low = msg.lower()
    for bad in _BAD_X_MARKERS:
        if bad in low:
            return False, f"reply contains disallowed marker: {bad!r}"
    if re.search(r"https?://\S+", msg):
        return False, "X reply must not contain links (triggers X spam suppression)"
    if "#" in msg:
        return False, "X reply must not contain hashtags (reads like bot spam)"
    if ro._PRICE_RE.search(msg):
        return False, "X reply #1 must not quote prices (help-first + soft DM invite)"
    return True, ""


def check_x_login():
    """Verify Chrome is logged in to x.com without modifying anything."""
    script = r"""
import json
tid = new_tab("https://x.com/home")
wait_for_load()
wait(3.5)
r = cdp("Runtime.evaluate", expression='''JSON.stringify({
  url: location.href,
  logged_in: !!document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"], [data-testid="tweetTextarea_0"]'),
  account: (document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]')?.innerText || '').replace(/\\s+/g, ' ').trim()
})''', returnByValue=True)
txt = r.get("result", {}).get("value", "")
try:
    cdp("Target.closeTarget", targetId=tid)
except Exception:
    pass
print("X_ME:" + txt)
"""
    ok, stdout, err = ro._run_harness(script, timeout=40)
    if not ok:
        return {"logged_in": False, "handle": "", "account": "", "error": err}
    m = re.search(r"X_ME:(\{.*\})", stdout)
    if not m:
        return {"logged_in": False, "handle": "", "account": "", "error": "no response from x.com"}
    d = json.loads(m.group(1))
    hm = re.search(r"(@[A-Za-z0-9_]+)", d.get("account", ""))
    d["handle"] = hm.group(1) if hm else ""
    return d


def fetch_x_pains(queries=None, per_query=8):
    """Search x.com live tweets for pain signals via logged-in Chrome. Returns raw tweet dicts."""
    qs = list(queries) if queries else list(DEFAULT_QUERIES)
    payload = json.dumps({"queries": qs, "per_query": int(per_query)})
    script = f"JOB = {payload}\n" + r"""
import json
from urllib.parse import quote

def ev(expr):
    r = cdp("Runtime.evaluate", expression=expr, returnByValue=True)
    return r.get("result", {}).get("value")

all_tweets = []
seen = set()
for q in JOB["queries"]:
    url = f"https://x.com/search?q={quote(q)}&src=typed_query&f=live"
    tid = new_tab(url)
    wait_for_load()
    wait(4.0)
    raw = ev('''JSON.stringify(
      [...document.querySelectorAll('article[data-testid="tweet"]')].map(a => {
        const linkEl = a.querySelector('a[href*="/status/"]');
        const href = linkEl ? linkEl.href : '';
        const userTxt = (a.querySelector('[data-testid="User-Name"]')?.innerText || '').replace(/\\n/g, ' ').trim();
        const text = (a.querySelector('[data-testid="tweetText"]')?.innerText || '').replace(/\\s+/g, ' ').trim();
        const timeEl = a.querySelector('time');
        const dt = timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText || '') : '';
        return {user: userTxt, text, url: href, time: dt};
      })
    )''')
    try:
        cdp("Target.closeTarget", targetId=tid)
    except Exception:
        pass
    try:
        rows = json.loads(raw or "[]")
    except Exception:
        rows = []
    added = 0
    for row in rows:
        u = (row.get("url") or "").split("?")[0]
        txt = (row.get("text") or "").strip()
        if not u or "/status/" not in u or not txt or u in seen:
            continue
        seen.add(u)
        row["url"] = u
        row["query"] = q
        all_tweets.append(row)
        added += 1
        if added >= JOB["per_query"]:
            break
    wait(1.5)

print("X_TWEETS:" + json.dumps(all_tweets))
"""
    ok, stdout, err = ro._run_harness(script, timeout=max(60, 25 * len(qs)))
    if not ok:
        raise RuntimeError(f"X harvest failed: {err}")
    m = re.search(r"X_TWEETS:(\[.*\])", stdout, re.S)
    if not m:
        return []
    return json.loads(m.group(1))


def tweet_to_item(t):
    """Convert raw tweet dict to IdeaPulse save_items() 6-tuple."""
    text = (t.get("text") or "").strip()
    url = (t.get("url") or "").strip()
    if not text or "/status/" not in url:
        return None
    hm = re.search(r"x\.com/([A-Za-z0-9_]+)/status/", url)
    handle = ("@" + hm.group(1)) if hm else ""
    if not handle:
        hm2 = re.search(r"(@[A-Za-z0-9_]+)", t.get("user") or "")
        handle = hm2.group(1) if hm2 else ""
    title = text[:160] + ("…" if len(text) > 160 else "")
    when = (t.get("time") or "")[:16].replace("T", " ")
    meta = " · ".join(x for x in ("X live", handle, when) if x)
    return (title[:200], url, meta[:120], "pain", text, handle)


def harvest_x_pains(queries=None, per_query=8, fetch_fn=fetch_x_pains):
    """Fetch live pain tweets and save new ones into `items` as `source='X pains'`."""
    raw = fetch_fn(queries=queries, per_query=per_query)
    items = [it for it in (tweet_to_item(t) for t in raw) if it]
    saved = ip.save_items(SOURCE_NAME, items) if items else 0
    ip.set_status(SOURCE_NAME, "ok", saved)
    return {"fetched": len(items), "saved_new": saved, "sample": [i[0][:100] for i in items[:5]]}


def build_x_review_packet(item_id):
    conn = ro._conn()
    try:
        item = conn.execute(
            "SELECT id, title, url, meta, source, author, body FROM items WHERE id=?", (item_id,)
        ).fetchone()
        if not item:
            return {"ok": False, "error": f"Item {item_id} not found"}
        pain = conn.execute(
            "SELECT status, persona, job, workaround, cost_signal, pay_signal, quote FROM pains WHERE item_id=?",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()

    lines = [
        X_CRITIC_SYSTEM,
        "",
        f"Item ID: {item['id']} | Source: {item['source']} | Author: {item['author'] or 'unknown'}",
        f"Tweet URL: {item['url']}",
        f"Tweet text: {item['body'] or item['title']}",
    ]
    if pain:
        lines.extend([
            f"Extracted persona: {pain['persona']}",
            f"Extracted job: {pain['job']}",
            f"Pay signal: {pain['pay_signal']}",
        ])
    return {"ok": True, "item_id": item_id, "prompt": "\n".join(lines)}


def ask_x_critic(prompt, critic="auto", timeout=60):
    if critic == "llm":
        import llm
        text, model = llm.chat(X_CRITIC_SYSTEM, prompt, max_tokens=800, timeout=90, want_json=False)
        if not text or not ro.parse_critic_reply(text)["parsed"]:
            return "", model or "llm", f"unusable llm reply ({model})"
        return text, model, ""
    if critic == "gemini":
        return ro._ask_gemini(prompt, timeout=timeout)
    if critic == "auto":
        reply, model, err = ro._ask_gemini(prompt, timeout=timeout)
        if not err:
            return reply, model, ""
        return ask_x_critic(prompt, critic="llm", timeout=90)
    return "", critic, f"unsupported critic '{critic}'"


def post_x_reply(tweet_url, message):
    """Post a reply on an x.com status page via logged-in Chrome."""
    payload = json.dumps({"url": tweet_url, "message": message})
    script = f"JOB = {payload}\n" + r"""
import json
tid = new_tab(JOB["url"])
wait_for_load()
wait(3.5)

def ev(expr):
    r = cdp("Runtime.evaluate", expression=expr, returnByValue=True)
    return r.get("result", {}).get("value")

st = json.loads(ev('''JSON.stringify({
  loggedIn: !!document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]'),
  account: (document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]')?.innerText || '').replace(/\\s+/g, ' ').trim(),
  hasTweet: !!document.querySelector('article[data-testid="tweet"]'),
  boxRect: (() => {
    const el = document.querySelector('[data-testid="tweetTextarea_0"]');
    if (!el) return null;
    el.scrollIntoView({block: 'center'});
    const r = el.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
  })()
})'''))

result = {"ok": False, "url": JOB["url"], "account": st.get("account", "")}
if not st.get("loggedIn"):
    result["error"] = "Not logged in to X in Chrome"
elif not st.get("hasTweet"):
    result["error"] = "Tweet not found or deleted"
elif not st.get("boxRect"):
    result["error"] = "Reply composer not found (replies may be restricted)"
else:
    click(st["boxRect"]["x"], st["boxRect"]["y"])
    wait(0.6)
    type_text(JOB["message"])
    wait(0.8)
    btn = json.loads(ev('''JSON.stringify((() => {
      const b = document.querySelector('[data-testid="tweetButtonInline"], [data-testid="tweetButton"]');
      const txt = (document.querySelector('[data-testid="tweetTextarea_0"]')?.innerText || '').trim();
      if (!b) return {found: false, txt};
      const disabled = b.disabled || b.getAttribute('aria-disabled') === 'true';
      const r = b.getBoundingClientRect();
      return {found: true, disabled, txt, x: r.left + r.width / 2, y: r.top + r.height / 2};
    })())'''))
    if not btn.get("txt"):
        result["error"] = "Failed to insert reply text into X composer"
    elif not btn.get("found") or btn.get("disabled"):
        result["error"] = "Reply button missing or disabled"
    else:
        click(btn["x"], btn["y"])
        wait(3.5)
        after = json.loads(ev('''JSON.stringify({
          boxNow: (document.querySelector('[data-testid="tweetTextarea_0"]')?.innerText || '').trim(),
          toast: (document.querySelector('[data-testid="toast"]')?.innerText || '').trim(),
          alert: (document.querySelector('[role="alert"]')?.innerText || '').trim()
        })'''))
        err_txt = after.get("alert") or ""
        if err_txt and any(w in err_txt.lower() for w in ("limit", "suspended", "locked", "error", "try again", "automated")):
            result["error"] = "X warning/error: " + err_txt
        elif after.get("boxNow") == "" or "sent" in (after.get("toast") or "").lower():
            result["ok"] = True
            result["posted_url"] = JOB["url"]
        else:
            result["error"] = "Reply box did not clear after click (toast: " + (after.get("toast") or "none") + ")"

try:
    cdp("Target.closeTarget", targetId=tid)
except Exception:
    pass
print("X_POST:" + json.dumps(result))
"""
    ok, stdout, err = ro._run_harness(script, timeout=55)
    if not ok:
        return {"ok": False, "error": err}
    m = re.search(r"X_POST:(\{.*\})", stdout)
    if not m:
        return {"ok": False, "error": "no post result returned by browser-harness"}
    return json.loads(m.group(1))


def candidate_x_items(limit=10):
    """Uncontacted X pain tweets (excluding any marked BUILDER/NOT_PAIN)."""
    conn = ro._conn()
    try:
        rows = conn.execute(
            "SELECT i.id, i.title, i.url, i.meta, i.author, i.body, p.status AS pain_status "
            "FROM items i LEFT JOIN pains p ON p.item_id = i.id "
            "WHERE i.source = ? AND (p.status IS NULL OR p.status = 'PAIN') "
            "ORDER BY i.id DESC LIMIT 120",
            (SOURCE_NAME,),
        ).fetchall()
        out = []
        for r in rows:
            if ro.already_contacted(conn, r["id"], r["author"], r["url"]):
                continue
            out.append({
                "id": r["id"],
                "author": r["author"] or "",
                "text": (r["body"] or r["title"] or ""),
                "url": r["url"] or "",
                "meta": r["meta"] or "",
            })
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


def reply_to_tweet(
    item_id,
    send=False,
    message=None,
    critic="auto",
    daily_cap=DAILY_CAP,
    critic_fn=ask_x_critic,
    post_fn=post_x_reply,
    humanize_fn=None,
):
    """Critic-gate, humanize, and (with send=True) post a single helpful reply on X."""
    import humanizer
    conn = ro._conn()
    try:
        item = conn.execute(
            "SELECT id, title, url, author, source, body FROM items WHERE id=?", (item_id,)
        ).fetchone()
        if not item:
            return {"ok": False, "error": f"Item {item_id} not found", "code": 1}
        if "/status/" not in (item["url"] or ""):
            return {"ok": False, "error": f"Item {item_id} is not an X status URL ({item['url']})", "code": 2}
        pain = conn.execute("SELECT status FROM pains WHERE item_id=?", (item_id,)).fetchone()
        if pain and pain["status"] != "PAIN":
            return {"ok": False, "error": f"Item {item_id} has pain status {pain['status']} — refusing", "code": 2}

        dup = ro.already_contacted(conn, item_id, item["author"], item["url"])
        if dup:
            return {"ok": False, "error": f"Duplicate guard: {dup}", "code": 2}

        used_today = ro.sent_today(conn, "x")
        if send and used_today >= daily_cap:
            return {"ok": False, "error": f"Daily X cap reached ({used_today}/{daily_cap}). Try again tomorrow.", "code": 2}
    finally:
        conn.close()

    if message is not None:
        parsed = {"parsed": True, "common_sense": "manual override", "verdict": "GO",
                  "reason": "manual message override", "offer": "", "message": message.strip()}
        critic_model = "manual"
    else:
        pkt = build_x_review_packet(item_id)
        if not pkt.get("ok"):
            return {"ok": False, "error": pkt.get("error", "packet error"), "code": 1}
        raw_reply, critic_model, err = critic_fn(pkt["prompt"], critic=critic)
        if err:
            return {"ok": False, "error": f"Critic ({critic}) failed: {err}", "code": 3}
        parsed = ro.parse_critic_reply(raw_reply)
        if not parsed["parsed"]:
            return {"ok": False, "error": f"Critic ({critic_model}) returned unparseable output", "code": 3}
        if parsed["verdict"] != "GO":
            return {
                "ok": True,
                "action": "skipped",
                "item_id": item_id,
                "author": item["author"] or "",
                "thread_url": item["url"],
                "critic_model": critic_model,
                "common_sense": parsed.get("common_sense", ""),
                "verdict": parsed["verdict"],
                "reason": parsed["reason"],
            }

    if humanize_fn is not None:
        hum = humanize_fn(parsed["message"])
    else:
        rw = None if message is not None else humanizer._default_rewrite
        hum = humanizer.humanize_reply(parsed["message"], max_chars=220, rewrite_fn=rw)
    final_msg = hum["text"]

    valid, verr = validate_x_reply(final_msg)
    if not valid:
        return {"ok": False, "error": f"X reply failed hygiene check: {verr}", "code": 2,
                "draft": final_msg}

    if send and hum["verdict"] != "PASS":
        return {
            "ok": False,
            "error": f"Humanizer gate blocked X send ({hum['human_score']:.1f} {hum['verdict']}; weakest: {hum['weakest']}). Rewrite before sending.",
            "code": 2,
            "draft": final_msg,
            "human_score": hum["human_score"],
            "human_verdict": hum["verdict"],
            "human_weakest": hum["weakest"],
        }

    out = {
        "ok": True,
        "action": "dry_run",
        "item_id": item_id,
        "author": item["author"] or "",
        "thread_url": item["url"],
        "critic_model": critic_model,
        "common_sense": parsed.get("common_sense", ""),
        "verdict": parsed["verdict"],
        "reason": parsed["reason"],
        "offer": parsed["offer"],
        "message": final_msg,
        "human_score": hum["human_score"],
        "human_verdict": hum["verdict"],
        "human_weakest": hum["weakest"],
        "sent_today": used_today,
        "daily_cap": daily_cap,
    }
    if not send:
        return out

    post_res = post_fn(item["url"], final_msg)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not post_res.get("ok"):
        conn = ro._conn()
        try:
            conn.execute(
                "INSERT INTO outreach_log (item_id, prospect_id, channel, thread_url, author, message, "
                "critic_model, critic_verdict, posted_url, status, error, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, None, "x", item["url"], item["author"] or "", final_msg,
                 critic_model, parsed["verdict"], "", "error", post_res.get("error", "X post failed"), now),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": False, "error": post_res.get("error", "X post failed"), "code": 4, "thread_url": item["url"]}

    fu = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    staged = cli.add_lead(item_id, followup=fu, extra_notes=f"Sent via X ({critic_model})")
    pid = staged.get("prospect_id") if staged.get("ok") else None
    if pid is not None:
        cli.mark_prospect(pid, "contacted")

    conn = ro._conn()
    try:
        conn.execute(
            "INSERT INTO outreach_log (item_id, prospect_id, channel, thread_url, author, message, "
            "critic_model, critic_verdict, posted_url, status, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, pid, "x", item["url"], item["author"] or "", final_msg,
             critic_model, parsed["verdict"], post_res.get("posted_url", item["url"]), "sent", "", now),
        )
        conn.commit()
    finally:
        conn.close()

    out.update({
        "action": "sent",
        "prospect_id": pid,
        "posted_url": post_res.get("posted_url", item["url"]),
        "posted_as": post_res.get("account", ""),
        "followup": fu,
        "sent_today": used_today + 1,
    })
    return out


def run_x_batch(
    limit=3,
    send=False,
    critic="auto",
    daily_cap=DAILY_CAP,
    gap_s=30.0,
    critic_fn=ask_x_critic,
    post_fn=post_x_reply,
    sleep_fn=time.sleep,
    humanize_fn=None,
):
    conn = ro._conn()
    try:
        used = ro.sent_today(conn, "x")
    finally:
        conn.close()

    remaining = max(0, daily_cap - used)
    if send and remaining == 0:
        return {
            "ok": False,
            "error": f"Daily X cap already reached ({used}/{daily_cap}).",
            "sent_today": used,
            "daily_cap": daily_cap,
            "results": [],
        }

    target = min(limit, remaining) if send else limit
    candidates = candidate_x_items(limit=max(target * 2, 6))
    results = []
    sent_count = 0

    for cand in candidates:
        if send and sent_count >= target:
            break
        if not send and len(results) >= target:
            break
        r = reply_to_tweet(
            cand["id"],
            send=send,
            critic=critic,
            daily_cap=daily_cap,
            critic_fn=critic_fn,
            post_fn=post_fn,
            humanize_fn=humanize_fn,
        )
        results.append(r)
        if not r.get("ok"):
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
