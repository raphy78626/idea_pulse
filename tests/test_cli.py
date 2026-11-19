"""Tests for ideapulse_cli.py (Hermes Agent + terminal interface). No network."""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as ideapulse  # noqa: E402
import ideapulse_cli as cli  # noqa: E402
import money  # noqa: E402
import pain_extract  # noqa: E402
import pains_page  # noqa: E402

PAIN_BODY = (
    "I run a dental clinic with 3 doctors. Every evening we spend 2 hours manually "
    "confirming WhatsApp appointments. I'd gladly pay ₹2,000/mo to stop doing this."
)
BUILDER_BODY = (
    "We built a WhatsApp bot for dental clinics after seeing clinics spend 2 hours manually "
    "confirming appointments."
)


def _reply(**kw):
    d = {
        "author_role": "has_pain",
        "is_pain": True,
        "persona": "3-doctor dental clinic owner",
        "job_to_be_done": "confirm evening WhatsApp appointments automatically",
        "current_workaround": "manual replies for 2 hours every evening",
        "tools_mentioned": ["WhatsApp"],
        "cost_signal": "2 hours every evening",
        "pay_signal": "would gladly pay ₹2,000/mo",
        "quote": "Every evening we spend 2 hours manually confirming WhatsApp appointments",
    }
    d.update(kw)
    return json.dumps(d)


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ideas.db"
    for mod in (ideapulse, money, pains_page):
        monkeypatch.setattr(mod, "DB", db_path)
    monkeypatch.setattr(ideapulse, "seed_from_csv", lambda: None)

    ideapulse.save_items("Reddit pains", [
        ("Manually confirming clinic appointments",
         "https://www.reddit.com/r/smallbusiness/comments/p1/",
         "r/smallbusiness · 12 ↑ · 8 comments", "pain", PAIN_BODY, "dr_patel"),
        ("We built a dental clinic bot",
         "https://www.reddit.com/r/SaaS/comments/b1/",
         "r/SaaS · 5 ↑ · 3 comments", "pain", BUILDER_BODY, "rival_dev"),
    ])
    ideapulse.save_items("Reddit business ideas", [
        ("Bookkeeping cleanup sprint",
         "https://www.reddit.com/r/smallbusiness/comments/m1/",
         "r/smallbusiness · 22 ↑", "business"),
    ])
    conn = ideapulse.db()
    for item_id, title, body in pain_extract.pending(conn):
        rep = _reply(author_role="builder", is_pain=False,
                     quote="seeing clinics spend 2 hours manually confirming appointments") if "We built" in title else _reply()
        pain_extract.save(conn, item_id, pain_extract.extract_one(title, body, chat=lambda s, u, **k: (rep, "test/m")))
    conn.execute(
        "UPDATE items SET earn_verdict='GO', what='1-week bookkeeping cleanup', buyer='small clinics', "
        "offer_money='₹4,999', first_payment='5', capital=0, proof='yes' WHERE source='Reddit business ideas'"
    )
    conn.commit()
    pain_id = conn.execute("SELECT item_id FROM pains WHERE status='PAIN'").fetchone()[0]
    builder_id = conn.execute("SELECT item_id FROM pains WHERE status='BUILDER'").fetchone()[0]
    conn.close()
    return {"pain_id": pain_id, "builder_id": builder_id}


def run_json(argv, capsys):
    rc = cli.main(argv + ["--json"])
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_status_and_brief_json(seeded_db, capsys):
    rc, st = run_json(["status", "--no-server"], capsys)
    assert rc == 0
    assert st["pains"]["PAIN"] == 1
    assert st["pains"]["BUILDER"] == 1
    assert st["money"]["GO"] == 1
    assert st["outreach"]["total"] == 0

    rc2, br = run_json(["brief", "--no-server"], capsys)
    assert rc2 == 0
    assert len(br["top_pains"]) == 1
    assert br["top_pains"][0]["author"] == "u/dr_patel"
    assert len(br["top_go_offers"]) == 1
    assert br["top_go_offers"][0]["what"] == "1-week bookkeeping cleanup"


def test_pains_and_money_cli_exclude_builders(seeded_db, capsys):
    rc, pains = run_json(["pains", "--status", "pain"], capsys)
    assert rc == 0 and len(pains) == 1
    assert pains[0]["id"] == seeded_db["pain_id"]
    assert pains[0]["in_outreach"] is False

    rc2, offers = run_json(["money", "--lane", "pains"], capsys)
    assert rc2 == 0 and len(offers) == 1
    assert offers[0]["id"] == seeded_db["pain_id"]


def test_add_lead_stages_pain_is_idempotent_and_refuses_builder(seeded_db, capsys):
    # Refuses BUILDER with exit code 2
    rc_bad, err = run_json(["add-lead", str(seeded_db["builder_id"])], capsys)
    assert rc_bad == 2
    assert err["ok"] is False
    assert "BUILDER" in err["error"]

    # Stages verified PAIN cleanly
    today = datetime.now().strftime("%Y-%m-%d")
    rc1, res1 = run_json(["add-lead", str(seeded_db["pain_id"]), "--followup", today], capsys)
    assert rc1 == 0
    assert res1["ok"] is True and res1["already_exists"] is False
    assert res1["name"] == "u/dr_patel"
    assert res1["template"] == "pain_reply"
    assert "confirm evening WhatsApp appointments automatically" in res1["draft_message"]

    # Calling add-lead again for the same pain is idempotent (no duplicate created)
    rc2, res2 = run_json(["add-lead", str(seeded_db["pain_id"])], capsys)
    assert rc2 == 0
    assert res2["already_exists"] is True
    assert res2["prospect_id"] == res1["prospect_id"]

    # Appears in outreach --due and can be marked contacted
    rc3, due = run_json(["outreach", "--due"], capsys)
    assert rc3 == 0 and len(due) == 1
    assert due[0]["thread_url"] == "https://www.reddit.com/r/smallbusiness/comments/p1/"

    rc4, mk = run_json(["mark", str(res1["prospect_id"]), "contacted"], capsys)
    assert rc4 == 0
    assert mk["old_status"] == "new" and mk["new_status"] == "contacted"

    # Pains list now reflects in_outreach=True
    rc5, pains = run_json(["pains", "--status", "pain"], capsys)
    assert rc5 == 0 and pains[0]["in_outreach"] is True
