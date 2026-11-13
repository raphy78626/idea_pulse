"""Tests for the access-code gate on the shared Cloudflare link. No network."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402
import share_gate  # noqa: E402

CF = {"cf-ray": "abc123-BOM", "cf-connecting-ip": "203.0.113.9"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    monkeypatch.setattr(share_gate, "SHARE_FILE", tmp_path / ".share_access")
    ideapulse.db().close()
    from fastapi.testclient import TestClient
    return TestClient(ideapulse.app, base_url="https://x.trycloudflare.com")


def test_local_requests_never_see_the_gate(client):
    assert client.get("/").status_code == 200


def test_tunnel_with_no_code_file_serves_nothing(client):
    r = client.get("/", headers=CF)
    assert r.status_code == 403 and "closed" in r.text


def test_tunnel_asks_for_the_code(client):
    share_gate.new_code(1)
    r = client.get("/pains", headers=CF)
    assert r.status_code == 401 and "Enter the access code" in r.text


def test_wrong_code_is_refused(client):
    share_gate.new_code(1)
    r = client.post("/share/login", data={"code": "nope"}, headers=CF)
    assert r.status_code == 401 and "not right" in r.text


def test_right_code_opens_every_page(client):
    code, _ = share_gate.new_code(1)
    r = client.post("/share/login", data={"code": code.upper()}, headers=CF, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", headers=CF).status_code == 200
    assert client.get("/simulations", headers=CF).status_code == 200


def test_expired_code_closes_the_link(client):
    code, _ = share_gate.new_code(1)
    client.post("/share/login", data={"code": code}, headers=CF)
    share_gate.SHARE_FILE.write_text(json.dumps({"code": code, "expires": time.time() - 1}))
    r = client.get("/", headers=CF)
    assert r.status_code == 403


def test_ending_the_share_closes_the_link(client):
    code, _ = share_gate.new_code(1)
    client.post("/share/login", data={"code": code}, headers=CF)
    share_gate.end()
    assert client.get("/", headers=CF).status_code == 403
