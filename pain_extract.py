"""Stage 2 of PLAN_FIRST_DOLLAR.md: turn each pain post into a structured pain.

One free-model call per post returns who has the problem, the job they are
trying to get done, how they do it today, what it costs them, any sign they
would pay, and one quote. Nothing the model says is trusted on its own:

  - the quote must appear in the post (ignoring case, whitespace and curly
    quotes), otherwise it is dropped and the pain is marked UNVERIFIED;
  - tools the post never names are removed from tools_mentioned.

Statuses written to the `pains` table:
  PAIN        real pain, quote found in the post
  UNVERIFIED  the model called it a pain but its quote is not in the post
  BUILDER     the author is building, researching or selling a fix for the pain:
              not a lead, but evidence the pain is real and a rival to count
  NOT_PAIN    self-promotion, news, general advice, no concrete problem
  ERROR       the model call failed; retried on the next run, never buried
"""
import json
import re
from datetime import datetime

MAX_BODY = 4000          # characters of post body sent to the model
MIN_QUOTE_WORDS = 4

SYSTEM = """You read one Reddit post and decide whether its author describes a
concrete problem they have in their own work or business. Reply with JSON only:
{"author_role": "has_pain" | "builder" | "other",
 "is_pain": true|false,
 "persona": "who the author is, e.g. 'Shopify seller doing ~$20k/month'",
 "job_to_be_done": "what they are trying to get done, one line",
 "current_workaround": "how they do it today, one line, or ''",
 "tools_mentioned": ["product names the post itself mentions"],
 "cost_signal": "time or money the problem costs them, as stated, or ''",
 "pay_signal": "any sign they would pay or already pay for a fix, or ''",
 "quote": "ONE sentence copied EXACTLY, character for character, from the post that best shows the pain"}

author_role is "builder" when the author is building, researching, validating or
selling a fix for the problem rather than suffering it: "I'm researching a tool
for...", "we've been building...", "I got tired of X so I built Y", a launch told
as a personal story, an agency pitching its service. For a builder, still fill
job_to_be_done with the problem they target. "has_pain" is an author describing
their own problem; "other" is anything else.

is_pain is false for: self-promotion or launches, news, hiring, selling services,
general strategy questions with no concrete problem, and complaints about
customers or the market that no tool could fix.
Never invent facts. Use '' when the post does not say. The quote must be copied
from the post, not paraphrased."""


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS pains (
        item_id INTEGER PRIMARY KEY REFERENCES items(id),
        status TEXT, persona TEXT, job TEXT, workaround TEXT, tools TEXT,
        cost_signal TEXT, pay_signal TEXT, quote TEXT,
        model TEXT, error TEXT, extracted_on TEXT)""")


def _norm(text):
    text = (text or "").lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def verify_quote(quote, source):
    """True if quote is a real excerpt of source and long enough to mean something."""
    q = _norm(quote).strip(" \"'.,!?;:")
    if len(q.split()) < MIN_QUOTE_WORDS:
        return False
    return q in _norm(source)


def _as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1")


def _text(v, limit=300):
    return str(v or "").strip()[:limit]


def extract_one(title, body, chat=None):
    """Structured pain for one post. Always returns a dict with a status."""
    if chat is None:
        import llm
        chat = llm.chat
    import llm as _llm  # parse_json only; no network
    source = (title or "") + "\n" + (body or "")
    user = "Title: %s\n\nPost:\n%s" % (title or "", (body or "")[:MAX_BODY] or "(no body)")
    text, model = chat(SYSTEM, user, max_tokens=1500, want_json=True)
    d = _llm.parse_json(text)
    if not d:
        return {"status": "ERROR", "error": model or "no json", "model": "",
                "persona": "", "job": "", "workaround": "", "tools": [],
                "cost_signal": "", "pay_signal": "", "quote": ""}
    tools = [t for t in (d.get("tools_mentioned") or []) if isinstance(t, str)
             and t.strip() and _norm(t) in _norm(source)]
    quote = _text(d.get("quote"), 500)
    verified = verify_quote(quote, source)
    if str(d.get("author_role") or "").strip().lower() == "builder":
        # a rival working on this pain: evidence the pain is real, and competition
        status = "BUILDER"
        quote = quote if verified else ""
    elif not _as_bool(d.get("is_pain")):
        status = "NOT_PAIN"
    elif verified:
        status = "PAIN"
    else:
        status, quote = "UNVERIFIED", ""
    return {"status": status, "error": "", "model": model,
            "persona": _text(d.get("persona")), "job": _text(d.get("job_to_be_done")),
            "workaround": _text(d.get("current_workaround")), "tools": tools,
            "cost_signal": _text(d.get("cost_signal")), "pay_signal": _text(d.get("pay_signal")),
            "quote": quote}


def pending(conn, limit=None, redo=False):
    """Pain posts with no extraction yet, or whose last attempt was an ERROR.

    redo=True returns every pain post, for re-running after a prompt change.
    """
    ensure_table(conn)
    todo = "" if redo else " AND (p.item_id IS NULL OR p.status = 'ERROR')"
    sql = ("SELECT i.id, i.title, i.body FROM items i LEFT JOIN pains p ON p.item_id = i.id"
           " WHERE i.kind = 'pain'" + todo + " ORDER BY i.id DESC")
    if limit:
        sql += " LIMIT %d" % int(limit)
    return conn.execute(sql).fetchall()


def save(conn, item_id, p):
    conn.execute(
        "INSERT OR REPLACE INTO pains (item_id, status, persona, job, workaround, tools,"
        " cost_signal, pay_signal, quote, model, error, extracted_on)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, p["status"], p["persona"], p["job"], p["workaround"], json.dumps(p["tools"]),
         p["cost_signal"], p["pay_signal"], p["quote"], p["model"], p["error"],
         datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()


def run(conn, limit=40, chat=None, progress=None, redo=False):
    """Extract up to `limit` pending posts. Returns counts per status."""
    summary = {"done": 0, "PAIN": 0, "UNVERIFIED": 0, "BUILDER": 0, "NOT_PAIN": 0, "ERROR": 0}
    for item_id, title, body in pending(conn, limit, redo=redo):
        p = extract_one(title, body, chat=chat)
        save(conn, item_id, p)
        summary["done"] += 1
        summary[p["status"]] += 1
        if progress:
            progress(item_id, title, p)
    return summary
