"""First customer, Launch directories and Outreach pages render in the shared look. No network."""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402
import launch  # noqa: E402
import money  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for mod in (ideapulse, money, launch):
        monkeypatch.setattr(mod, "DB", tmp_path / "ideas.db")
    monkeypatch.setattr(ideapulse, "seed_from_csv", lambda: None)
    ideapulse.db().close()
    from fastapi.testclient import TestClient
    return TestClient(ideapulse.app)


def test_every_page_shares_the_header(client):
    for path in ("/", "/pains", "/money", "/launch", "/outreach"):
        t = client.get(path).text
        assert '<nav class="links" aria-label="Pages">' in t, path
        assert t.count("aria-current=page>") >= 1, path


# ---- First customer ----------------------------------------------------------

def seed_offer(**cols):
    ideapulse.save_items("Reddit business ideas", [
        ("Bookkeeping for dentists", "https://www.reddit.com/r/x/comments/1/", "r/x · 5 ↑", "business")])
    conn = ideapulse.db()
    base = {"earn_verdict": "GO", "first_payment": "7", "capital": 0, "proof": "yes",
            "what": "monthly bookkeeping", "buyer": "dental clinics", "offer_money": "₹5,000 a month"}
    base.update(cols)
    conn.execute("UPDATE items SET " + ", ".join(f"{k}=?" for k in base), list(base.values()))
    conn.commit(); conn.close()


def test_money_go_offer_reads_as_a_sentence(client):
    seed_offer()
    t = client.get("/money").text
    assert "1 offer you could sell this week." in t
    assert "Sell now." in t and "First payment in about 7 days, nothing to spend to start." in t
    assert "Someone earns from this today" in t and "dental clinics" in t


def test_money_first_payment_text_is_parsed_safely(client):
    seed_offer(first_payment="about 10 days", earn_verdict="LATER")
    assert "First payment in about 10 days" in client.get("/money").text


def test_money_buttons_have_the_progress_widget(client):
    """The page used to ship a literal @@JOBBAR@@, so JobBar was undefined and both buttons threw."""
    t = client.get("/money").text
    assert "@@JOBBAR@@" not in t
    assert 'id="jobbox"' in t and "window.JobBar" in t


# ---- Launch directories --------------------------------------------------------

def test_launch_checklist_counts_ticks(client):
    sites = launch._sites()
    t = client.get("/launch").text
    assert f"0 of {len(sites)} directories done." in t
    assert t.count('<div class="row') == len(sites)
    client.get("/launch/toggle/" + launch.slug(sites[0]["name"]) + "?product=", follow_redirects=False)
    assert f"1 of {len(sites)} directories done." in client.get("/launch").text


def test_launch_ideas_tab_has_the_progress_widget(client):
    t = client.get("/launch?tab=ideas").text
    assert 'id="jobbox"' in t and "Fetch directories" in t


# ---- Outreach ------------------------------------------------------------------

def add(client, **form):
    base = {"name": "Dr. Ashok", "business": "Maurya Dental", "phone": "9876543210",
            "template": "english_dental", "followup": "", "notes": "Indiranagar"}
    base.update(form)
    client.post("/outreach/add", data=base, follow_redirects=False)


def test_outreach_card_and_headline(client):
    add(client)
    t = client.get("/outreach").text
    assert "1 person in the pipeline. 0 to follow up today; 0 replied so far." in t
    assert "<h2>Dr. Ashok</h2>" in t and "Maurya Dental" in t
    assert 'aria-current="step">New</a>' in t
    assert "https://wa.me/919876543210?text=" in t


def test_outreach_copy_button_has_a_handler(client):
    """Cards called copyMsg(this), which was never defined."""
    add(client)
    t = client.get("/outreach").text
    assert 'onclick="copyMsg(this)"' in t and "async function copyMsg(el)" in t


def test_outreach_follow_up_today(client):
    add(client, followup=datetime.now().strftime("%Y-%m-%d"))
    t = client.get("/outreach").text
    assert "Follow up today" in t and "1 to follow up today" in t


def test_outreach_escapes_names(client):
    add(client, name="<script>x</script>")
    assert "<script>x</script>" not in client.get("/outreach").text
