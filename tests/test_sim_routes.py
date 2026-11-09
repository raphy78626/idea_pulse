"""Route tests for /simulate. The bridge is stubbed; no network or model."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    conn = ideapulse.db()
    conn.execute("INSERT INTO items (fetched_on, source, title, url, meta) VALUES (?,?,?,?,?)",
                 ("2026-11-09", "Reddit", "Test idea", "http://x/1", "some context"))
    conn.commit()
    conn.close()
    from fastapi.testclient import TestClient
    return TestClient(ideapulse.app)


def test_simulate_returns_error_when_bridge_rejects(client, monkeypatch):
    def boom(*args, **kwargs):
        raise ideapulse.mirofish_bridge.BridgeError("only 1 named market participant(s) found")
    monkeypatch.setattr(ideapulse.mirofish_bridge, "run_simulation", boom)
    r = client.post("/simulate/1")
    assert "named market participant" in r.text


def test_simulate_404_for_unknown_item(client):
    r = client.post("/simulate/999")
    assert "not found" in r.text.lower()


def test_simulate_reuses_existing_report_without_force(client, tmp_path, monkeypatch):
    out = tmp_path / "sim_1.md"
    out.write_text("# Report\n\n" + ("Body sentence. " * 30), encoding="utf-8")
    conn = ideapulse.db()
    conn.execute("UPDATE items SET sim_path=? WHERE id=1", (str(out),))
    conn.commit(); conn.close()
    called = {"n": 0}
    monkeypatch.setattr(ideapulse.mirofish_bridge, "run_simulation",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    r = client.get("/simulate/1")
    assert "existing" in r.json()["status"]
    assert called["n"] == 0


def test_simulation_view_renders_report(client, tmp_path):
    out = tmp_path / "sim_1.md"
    out.write_text("# Report\n\n" + ("Body sentence. " * 30), encoding="utf-8")
    conn = ideapulse.db()
    conn.execute("UPDATE items SET sim_path=? WHERE id=1", (str(out),))
    conn.commit(); conn.close()
    r = client.get("/simulation/1")
    assert r.status_code == 200
    assert "Body sentence" in r.text


def test_simulation_view_404_without_report(client):
    r = client.get("/simulation/1")
    assert r.status_code == 404


def test_simulation_columns_exist():
    conn = ideapulse.db()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)")]
    assert "sim_attempts" in cols
    assert "sim_error" in cols


def test_pending_simulations_selects_only_whitelisted_sources(client):
    conn = ideapulse.db()
    conn.execute("UPDATE items SET verdict='BUILD' WHERE id=1")
    conn.execute(
        "INSERT INTO items (fetched_on,source,title,url,meta,verdict) VALUES (?,?,?,?,?,?)",
        ("2026-11-09", "Show HN", "Show HN: my launch", "http://x/2", "ctx", "BUILD"))
    conn.commit(); conn.close()
    rows = ideapulse.pending_simulations(limit=10)
    ids = [r["id"] for r in rows]
    assert 1 not in ids, "Reddit is not in the simulatable whitelist by default"
    assert 2 not in ids, "launch feeds must never be simulated"


def test_pending_simulations_respects_attempt_cap(client):
    conn = ideapulse.db()
    conn.execute("UPDATE items SET source='Reddit business ideas', verdict='BUILD', sim_attempts=2 WHERE id=1")
    conn.commit(); conn.close()
    rows = ideapulse.pending_simulations(limit=10)
    assert [r["id"] for r in rows] == [], "items at the attempt cap must not be retried"


def test_pending_simulations_skips_completed(client):
    import pathlib, tempfile
    p = pathlib.Path(tempfile.mkdtemp()) / "sim_1.md"
    p.write_text("# R\n\n" + ("Body sentence. " * 30), encoding="utf-8")
    conn = ideapulse.db()
    conn.execute("UPDATE items SET source='Reddit business ideas', verdict='BUILD', sim_path=? WHERE id=1", (str(p),))
    conn.commit(); conn.close()
    rows = ideapulse.pending_simulations(limit=10)
    assert [r["id"] for r in rows] == [], "items with a usable report must be skipped"
