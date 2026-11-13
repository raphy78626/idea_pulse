"""Route tests for /simulate. The bridge is stubbed; no network or model."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    monkeypatch.setattr(ideapulse, "EVAL_DIR", tmp_path / "evaluations")
    # /simulate now validates the graph backend before doing work. The local
    # machine may have a placeholder ZEP key, so assume a configured backend
    # here; test_graph_preflight.py owns that check.
    monkeypatch.setattr(ideapulse.mirofish_bridge, "preflight", lambda *a, **k: (True, ""))
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


def test_simulate_job_progress_advances_with_real_stages(client, monkeypatch):
    """Regression: on_stage(pct, msg) must advance the job, not just its label.

    The route used to call label(msg) and drop pct, so a 6-minute simulation
    showed 0/4 and 0% the whole way. The progress bar must reflect the bridge's
    real stage percentages.
    """
    conn = ideapulse.db()
    conn.execute("UPDATE items SET verdict='BUILD' WHERE id=1")
    conn.commit(); conn.close()

    seen = []

    def fake_run(title, meta, source, on_stage=None, out_path=None, evaluation=""):
        # the real bridge emits (lo, label) per stage
        for pct, msg in ((0, "mapping the market"),
                         (30, "building the knowledge graph"),
                         (55, "running agent reactions"),
                         (85, "writing the report")):
            on_stage(pct, msg)
            seen.append(pct)
        import pathlib
        p = pathlib.Path(str(out_path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# Report\n\n" + ("Body sentence. " * 40), encoding="utf-8")
        return {"simulation_id": "sim_p", "report_path": str(p)}

    monkeypatch.setattr(ideapulse.mirofish_bridge, "preflight", lambda: (True, ""))
    monkeypatch.setattr(ideapulse.mirofish_bridge, "run_simulation", fake_run)

    # Record what `tick` the route calls at each stage. jobs.py forces
    # done=total on success, so asserting only the final state would let an
    # always-zero bar pass — this asserts the intermediate advances directly.
    import jobs as jobs_mod
    ticks = []
    real_start = jobs_mod.start

    def spy_start(label_text, total, fn, expect_s=None):
        def wrapped(tick, label):
            def recording_tick(n=1):
                ticks.append(n)
                tick(n)
            return fn(recording_tick, label)
        return real_start(label_text, total, wrapped, expect_s=expect_s)

    monkeypatch.setattr(jobs_mod, "start", spy_start)

    r = client.get("/simulate/1?async_=1")
    jid = r.json()["job"]

    import time as _t
    for _ in range(120):
        st = client.get("/jobs/" + jid).json()
        if st["state"] != "running":
            break
        _t.sleep(0.05)

    assert st["state"] == "done", st
    assert st["done"] == st["total"], "bar must reach the end"
    assert st["pct"] == 100
    assert seen == [0, 30, 55, 85], "bridge stage percentages must be forwarded"
    assert ticks, "on_stage must advance the job counter, not just its label"


def test_run_simulations_records_attempt_and_report(client, tmp_path, monkeypatch):
    conn = ideapulse.db()
    conn.execute("UPDATE items SET source='Reddit business ideas', verdict='BUILD' WHERE id=1")
    conn.commit(); conn.close()

    def fake_run(title, meta, source, on_stage=None, out_path=None, evaluation=""):
        import pathlib
        p = pathlib.Path(str(out_path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# Report\n\n" + ("Body sentence. " * 30), encoding="utf-8")
        return {"simulation_id": "sim_t", "report_path": str(p)}

    monkeypatch.setattr(ideapulse.mirofish_bridge, "preflight", lambda: (True, ""))
    monkeypatch.setattr(ideapulse.mirofish_bridge, "run_simulation", fake_run)
    result = ideapulse.run_simulations(limit=1, max_seconds=600)
    assert result["done"] == 1
    conn = ideapulse.db()
    row = conn.execute("SELECT sim_path, sim_attempts FROM items WHERE id=1").fetchone()
    conn.close()
    assert row[0], "sim_path must be stored"
    assert row[1] == 1, "attempt counter must increment"


def test_run_simulations_records_failure_and_increments_attempts(client, monkeypatch):
    conn = ideapulse.db()
    conn.execute("UPDATE items SET source='Reddit business ideas', verdict='BUILD' WHERE id=1")
    conn.commit(); conn.close()

    def boom(*a, **k):
        raise ideapulse.mirofish_bridge.BridgeError("only 1 named market participant(s) found")

    monkeypatch.setattr(ideapulse.mirofish_bridge, "preflight", lambda: (True, ""))
    monkeypatch.setattr(ideapulse.mirofish_bridge, "run_simulation", boom)
    result = ideapulse.run_simulations(limit=1, max_seconds=600)
    assert result["failed"] == 1
    conn = ideapulse.db()
    row = conn.execute("SELECT sim_attempts, sim_error FROM items WHERE id=1").fetchone()
    conn.close()
    assert row[0] == 1
    assert "named market participant" in row[1]


def test_run_simulations_stops_at_wall_clock_cap(client, monkeypatch):
    conn = ideapulse.db()
    for i in range(3):
        conn.execute(
            "INSERT INTO items (fetched_on,source,title,url,meta,verdict) VALUES (?,?,?,?,?,?)",
            ("2026-11-09", "Reddit business ideas", "idea %d" % i, "http://y/%d" % i, "c", "BUILD"))
    conn.commit(); conn.close()

    def slow(title, meta, source, on_stage=None, out_path=None, evaluation=""):
        raise ideapulse.mirofish_bridge.BridgeError("slow failure")

    monkeypatch.setattr(ideapulse.mirofish_bridge, "preflight", lambda: (True, ""))
    monkeypatch.setattr(ideapulse.mirofish_bridge, "run_simulation", slow)
    # no real sleeping: the cap is checked against a fake clock
    ticks = {"n": 0}
    real_time = ideapulse.time.time

    def fake_time():
        ticks["n"] += 1
        return real_time() + ticks["n"] * 400

    monkeypatch.setattr(ideapulse.time, "time", fake_time)
    result = ideapulse.run_simulations(limit=3, max_seconds=600)
    assert result["attempted"] < 3, "wall-clock cap must cut the pass short"


def test_run_simulations_refuses_when_mirofish_down(client, monkeypatch):
    monkeypatch.setattr(ideapulse.mirofish_bridge, "preflight",
                        lambda: (False, "MiroFish is not running. Start it with x"))
    result = ideapulse.run_simulations(limit=3, max_seconds=600)
    assert result["attempted"] == 0
    assert "MiroFish" in result["error"]


def test_run_simulations_writes_only_into_patchable_dir(tmp_path, monkeypatch):
    """Regression: the suite must not create files in the repo tree."""
    import pathlib
    repo_eval = pathlib.Path(ideapulse.__file__).parent / "evaluations"
    before = set(p.name for p in repo_eval.glob("sim_*.md")) if repo_eval.exists() else set()
    monkeypatch.setattr(ideapulse, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    conn = ideapulse.db()
    conn.execute("INSERT INTO items (fetched_on,source,title,url,meta,verdict) VALUES (?,?,?,?,?,?)",
                 ("2026-11-09", "Reddit business ideas", "t", "http://z/1", "c", "BUILD"))
    conn.commit(); conn.close()

    def fake_run(title, meta, source, on_stage=None, out_path=None, evaluation=""):
        p = pathlib.Path(str(out_path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# R\n\n" + ("Body sentence. " * 30), encoding="utf-8")
        return {"simulation_id": "s", "report_path": str(p)}

    monkeypatch.setattr(ideapulse.mirofish_bridge, "preflight", lambda: (True, ""))
    monkeypatch.setattr(ideapulse.mirofish_bridge, "run_simulation", fake_run)
    ideapulse.run_simulations(limit=1, max_seconds=600)

    after = set(p.name for p in repo_eval.glob("sim_*.md")) if repo_eval.exists() else set()
    assert after == before, "run_simulations must not write into the repo tree"
    assert list(tmp_path.glob("sim_*.md")), "it should write into EVAL_DIR instead"
