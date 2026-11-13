"""Tests for the two honesty checks on a simulation report, and the /simulations guide.

Real failure these pin down: the "Good for Bots" run (an AI-crawler URL scanner)
produced a 4,243-word report about Clay, Apollo and ZoomInfo and never once
mentioned crawlers. A reader of the old page had no way to notice.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402
import sim_explain  # noqa: E402

IDEA = ("A web tool that checks DMARC compliance and generates DNS records for domains "
        "blocked by Outlook.com")

ON_TOPIC = """# Report

> The simulation predicts a fragmented market response.

## Reactions

> "Outlook.com silently drops messages from domains that have a missing DMARC record."
> "IT and email administrators could use DMARC analysis services like dmarcian."
> "DNS hosts a DMARC record, which is an authentication entry required for email domains."
> "SaaS founders sending password resets hang out on IndieHackers."
> "Indian SMEs sending invoices hang out on IndiaMart."
> "Email deliverability analysts audit DMARC compliance for domains."

**dmarcian** pushed back on compliance pricing. Outlook.com kept rejecting domains.
"""

OFF_TOPIC = """# Report

> The simulation predicts a polarized market response.

> "Clay would publicly position against Apollo as part of a new orchestration layer."
> "ZoomInfo is an enterprise-grade contact-data provider that overlaps with Apollo."
> "Apollo sells exports used by mid-market revenue operations teams."
> "Clay is often adopted by teams frustrated with Apollo exports."
> "Revenue teams clean exports in spreadsheets by hand."
> "ZoomInfo and Apollo contracts overlap in sales orgs."

**Clay**, **Apollo** and **ZoomInfo** circle each other.
"""

THIN = """# Report

> The simulation predicts a skeptical market response.

The retrieved simulation contains only two current facts and four entities, and no
first-person statements from simulated buyers were available.

> "The DMARC tool may be offered for sale on Outlook.com domains."
> "DMARC compliance earnings grew fast for domains."
"""


# ---- did it stay on the idea? -------------------------------------------------

def test_on_topic_report_is_recognised():
    t = sim_explain.topic_check(ON_TOPIC, IDEA)
    assert t["level"] == "on"
    assert "dmarc" in t["found"]


def test_report_about_something_else_is_off_topic():
    t = sim_explain.topic_check(OFF_TOPIC, IDEA)
    assert t["level"] == "off"
    assert "dmarc" in t["missing"]


def test_filler_words_do_not_count_as_key_terms():
    terms = sim_explain.key_terms("A simple free tool that checks whether a website is good")
    assert terms == []


def test_no_idea_text_means_unknown_not_off():
    assert sim_explain.topic_check(ON_TOPIC, "")["level"] == "unknown"


# ---- did it have enough to go on? ------------------------------------------------

def test_thin_report_is_flagged_and_its_own_admission_is_caught():
    t = sim_explain.thin_check(THIN)
    assert t["thin"] and t["admits"] and t["facts"] == 2


def test_report_with_many_facts_is_not_thin():
    assert not sim_explain.thin_check(ON_TOPIC)["thin"]


# ---- one verdict per run ---------------------------------------------------------

def test_verdicts():
    assert sim_explain.run_verdict(ON_TOPIC, IDEA)["kind"] == "usable"
    off = sim_explain.run_verdict(OFF_TOPIC, IDEA)
    assert off["kind"] == "off_topic"
    assert "Clay" in off["say"] and "dmarc" in off["say"].lower()
    assert sim_explain.run_verdict(THIN, IDEA)["kind"] == "thin"


def test_off_topic_beats_thin():
    """A report about the wrong market is useless however many facts it has."""
    assert sim_explain.run_verdict(OFF_TOPIC + THIN.replace("DMARC", "Clay"), IDEA)["kind"] == "off_topic"


def test_intro_shows_the_check_and_links_the_guide():
    html = sim_explain.render(OFF_TOPIC, "Good for Bots", IDEA)
    assert "Went off topic" in html
    assert 'href="/simulations"' in html
    assert "not a forecast" in html.lower()
    assert "TimesFM" in html
    assert "simulated personas, not real" in html.lower()


# ---- the Simulate button must not write into the repo ----------------------------

def test_simulate_route_saves_into_eval_dir(tmp_path, monkeypatch):
    """Regression: /simulate built its own 'evaluations/' path, so tests wrote sim_1.md into the repo."""
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    monkeypatch.setattr(ideapulse, "EVAL_DIR", tmp_path / "evaluations")
    monkeypatch.setattr(ideapulse.mirofish_bridge, "preflight", lambda *a, **k: (True, ""))
    ideapulse.save_items("Reddit", [("Test idea", "http://x/1", "ctx", "post")])
    written = []

    def fake_run(title, meta, source, on_stage=None, out_path=None, evaluation=""):
        p = Path(str(out_path)); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# Report\n\n" + ("A sentence. " * 30), encoding="utf-8")
        written.append(p)
        return {"simulation_id": "s", "report_path": str(p)}
    monkeypatch.setattr(ideapulse.mirofish_bridge, "run_simulation", fake_run)
    from fastapi.testclient import TestClient
    TestClient(ideapulse.app).post("/simulate/1")
    assert written and written[0].parent == tmp_path / "evaluations"


# ---- the /simulations guide page -------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    ideapulse.save_items("hn_best (custom)", [
        ("Outlook.com Is Quietly Rejecting Email", "https://x/1", "m", "show_hn"),
        ("Good for Bots", "https://x/2", "m", "launch"),
    ])
    on = tmp_path / "sim_1.md"; on.write_text(ON_TOPIC)
    off = tmp_path / "sim_2.md"; off.write_text(OFF_TOPIC)
    conn = ideapulse.db()
    conn.execute("UPDATE items SET what=?, sim_path=? WHERE url='https://x/1'", (IDEA, str(on)))
    conn.execute("UPDATE items SET what=?, sim_path=? WHERE url='https://x/2'",
                 ("A URL scanner that checks robots.txt and whether AI crawlers can access a site",
                  str(off)))
    conn.commit(); conn.close()
    import sim_guide
    monkeypatch.setattr(sim_guide, "DB", tmp_path / "ideas.db")
    from fastapi.testclient import TestClient
    return TestClient(ideapulse.app)


def test_guide_explains_the_steps_and_checks_real_runs(client):
    t = client.get("/simulations").text
    assert "rehearsal, not a forecast" in t
    for step in ("Describe the idea", "Map the market", "Role-play", "Write it up"):
        assert step in t
    assert "Outlook.com Is Quietly Rejecting Email" in t and "Stayed on the idea" in t
    assert "Good for Bots" in t and "Went off topic" in t
    assert 'href="/simulation/' in t


def test_guide_engineer_numbers_come_from_the_code(client):
    import mirofish_bridge
    t = client.get("/simulations").text
    assert "%d seconds" % mirofish_bridge.STAGE_TIMEOUT_S in t
    assert "at least %d named" % mirofish_bridge.MIN_NAMED_ACTORS in t


def test_guide_with_no_runs_still_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "empty.db")
    import sim_guide
    monkeypatch.setattr(sim_guide, "DB", tmp_path / "empty.db")
    ideapulse.db().close()
    from fastapi.testclient import TestClient
    r = TestClient(ideapulse.app).get("/simulations")
    assert r.status_code == 200 and "No simulations have run yet" in r.text
