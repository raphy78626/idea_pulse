"""Tests for the slow simulation queue. No network, no models, no MiroFish.

The queue runs unattended, so its failure modes matter more than its happy path:
  - MiroFish down must NOT burn an attempt (the idea is fine, the env is not)
  - a finished report must never be redone
  - the attempt cap must eventually stop a hopeless idea
  - a failed simulation must record the error for later diagnosis
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as ideapulse  # noqa: E402
import sim_queue  # noqa: E402


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(ideapulse, "DB", tmp_path / "ideas.db")
    monkeypatch.setattr(ideapulse, "EVAL_DIR", tmp_path / "evaluations")
    monkeypatch.setattr(sim_queue, "EVAL_DIR", tmp_path / "evaluations")
    conn = ideapulse.db()
    for i, (src, verdict, build) in enumerate([
            ("Fazier", "BUILD", 9),
            ("Reddit business ideas", "BUILD", 8),
            ("HN Show HN", "BUILD", 7),
            ("Fazier", "WATCH", 6)], start=1):
        conn.execute(
            "INSERT INTO items (fetched_on, source, title, url, meta, verdict, buildability)"
            " VALUES (?,?,?,?,?,?,?)",
            ("2026-11-09", src, "Idea %d" % i, "http://x/%d" % i,
             "context for idea %d" % i, verdict, build))
    conn.commit()
    conn.close()
    return tmp_path


def test_queue_includes_all_build_sources(seeded):
    """The old whitelist excluded Fazier/Show HN; the queue must not."""
    rows = sim_queue.eligible()
    sources = {r["source"] for r in rows}
    assert "Fazier" in sources, "Fazier BUILD must be eligible"
    assert "HN Show HN" in sources, "Show HN BUILD must be eligible"
    assert len(rows) == 3, "only BUILD verdicts, not WATCH"


def test_queue_orders_by_buildability(seeded):
    rows = sim_queue.eligible()
    scores = [r["buildability"] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_skips_already_simulated(seeded):
    conn = ideapulse.db()
    conn.execute("UPDATE items SET sim_path='evaluations/sim_1.md' WHERE id=1")
    conn.commit()
    conn.close()
    ids = [r["id"] for r in sim_queue.eligible()]
    assert 1 not in ids


def test_respects_attempt_cap(seeded):
    conn = ideapulse.db()
    conn.execute("UPDATE items SET sim_attempts=? WHERE id=2", (sim_queue.MAX_ATTEMPTS,))
    conn.commit()
    conn.close()
    ids = [r["id"] for r in sim_queue.eligible()]
    assert 2 not in ids, "idea at the attempt cap must drop out"


def test_whitelist_flag_still_available(seeded):
    """Passing include_all_sources=False reproduces the old, narrower behaviour."""
    rows = sim_queue.eligible(include_all_sources=False)
    assert all(r["source"] == "Reddit business ideas" for r in rows)


def test_run_one_success_writes_path_and_increments(seeded, monkeypatch):
    def fake_run(title, meta, source, on_stage=None, out_path=None, evaluation=""):
        p = Path(str(out_path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# Report\n\n" + ("Sentence. " * 40), encoding="utf-8")
        return {"simulation_id": "x", "report_path": str(p)}

    monkeypatch.setattr(sim_queue.mirofish_bridge, "run_simulation", fake_run)
    row = sim_queue.eligible()[0]
    ok, detail = sim_queue.run_one(row)
    assert ok, detail
    conn = ideapulse.db()
    rec = conn.execute("SELECT sim_path, sim_attempts, sim_error FROM items WHERE id=?",
                       (row["id"],)).fetchone()
    conn.close()
    assert rec[0], "sim_path must be stored"
    assert rec[1] == 1
    assert rec[2] is None


def test_run_one_failure_records_error(seeded, monkeypatch):
    def boom(title, meta, source, on_stage=None, out_path=None, evaluation=""):
        raise sim_queue.mirofish_bridge.BridgeError("graph had no entities")

    monkeypatch.setattr(sim_queue.mirofish_bridge, "run_simulation", boom)
    row = sim_queue.eligible()[0]
    ok, detail = sim_queue.run_one(row)
    assert not ok
    assert "no entities" in detail
    conn = ideapulse.db()
    rec = conn.execute("SELECT sim_path, sim_attempts, sim_error FROM items WHERE id=?",
                       (row["id"],)).fetchone()
    conn.close()
    assert rec[0] is None, "a failure must not set sim_path"
    assert rec[1] == 1
    assert "no entities" in (rec[2] or "")


def test_main_exits_without_burning_attempts_when_mirofish_down(seeded, monkeypatch):
    """Environmental failure must not look like the idea's fault."""
    monkeypatch.setattr(sim_queue.mirofish_bridge, "preflight",
                        lambda: (False, "start mirofish_server.sh"))
    monkeypatch.setattr(sys, "argv", ["sim_queue.py", "--max", "1"])
    rc = sim_queue.main()
    assert rc == 2
    conn = ideapulse.db()
    n = conn.execute("SELECT COUNT(*) FROM items WHERE sim_attempts > 0").fetchone()[0]
    conn.close()
    assert n == 0, "no attempts should be recorded when the server is down"


def test_dry_run_touches_nothing(seeded, monkeypatch):
    def never(*a, **k):
        raise AssertionError("dry run must not call the bridge")

    monkeypatch.setattr(sim_queue.mirofish_bridge, "run_simulation", never)
    monkeypatch.setattr(sys, "argv", ["sim_queue.py", "--dry-run", "--max", "2"])
    assert sim_queue.main() == 0
    conn = ideapulse.db()
    n = conn.execute("SELECT COUNT(*) FROM items WHERE sim_attempts > 0").fetchone()[0]
    conn.close()
    assert n == 0
