"""The Outreach "Add a prospect" form must work without python-multipart.

The server runs on /usr/bin/python3, which does not have python-multipart, so
request.form() raised and every add returned a 500. The test venv does have it,
which hid the bug; here request.form() is made to fail the same way.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    monkeypatch.setattr(ideapulse, "seed_from_csv", lambda: None)

    async def no_multipart(self, *a, **k):
        raise AssertionError("The `python-multipart` library must be installed to use form parsing.")
    import starlette.requests
    monkeypatch.setattr(starlette.requests.Request, "form", no_multipart)
    from fastapi.testclient import TestClient
    return TestClient(ideapulse.app)


def test_add_prospect_without_python_multipart(client):
    r = client.post("/outreach/add", data={
        "name": "Dr. Mehta & Sons", "business": "Smile Dental", "phone": "",
        "template": "english_dental", "followup": "", "notes": "Pune, from IndiaMart"},
        follow_redirects=False)
    assert r.status_code == 303
    conn = ideapulse.out_db()
    row = conn.execute("SELECT name, business, template, followup, notes, status FROM prospects").fetchone()
    conn.close()
    assert row == ("Dr. Mehta & Sons", "Smile Dental", "english_dental", None, "Pune, from IndiaMart", "new")
