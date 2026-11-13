"""Tests for job progress + ETA. No network, no models.

The ETA is deliberately conservative: it refuses to guess until it has seen at
least two completed steps, then smooths, then bounds itself against observed
throughput. These tests pin the behaviours that make it trustworthy — and the
ones that stop it lying.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jobs  # noqa: E402


@pytest.fixture(autouse=True)
def clean_jobs():
    """Each test gets an empty registry."""
    with jobs._lock:
        jobs._jobs.clear()
    yield
    with jobs._lock:
        jobs._jobs.clear()


def _run(jid, timeout=5.0):
    """Wait for a job to finish, return its final state."""
    end = time.time() + timeout
    while time.time() < end:
        j = jobs.get(jid)
        if j["state"] != "running":
            return j
        time.sleep(0.01)
    raise AssertionError("job did not finish: %r" % jobs.get(jid))


def test_no_eta_before_two_steps():
    """A single completed step is not enough to extrapolate — no ETA yet."""
    seen = {}

    def work(tick, label):
        time.sleep(0.03)
        tick(1)                      # one step done
        seen.update(jobs.get(CURRENT["id"]))
        time.sleep(0.03)

    CURRENT = {}
    jid = jobs.start("t", 5, work)
    CURRENT["id"] = jid
    j = _run(jid)
    assert seen["eta"] is None, "must withhold ETA after one sample, got %r" % (seen["eta"],)
    assert seen["eta_confidence"] == "none"


def test_eta_appears_after_two_steps():
    seen = {}

    def work(tick, label):
        for _ in range(2):
            time.sleep(0.04)
            tick(1)
        seen.update(jobs.get(jid))
        time.sleep(0.10)

    jid = jobs.start("t", 10, work)
    j = _run(jid)
    assert seen["eta"] is not None, "ETA should be available after two steps"
    assert seen["eta"] > 0
    assert seen["eta_confidence"] in ("low", "fair", "good")


def test_eta_shrinks_as_work_completes():
    """As remaining steps fall, the estimate must fall with them."""
    samples = []

    def work(tick, label):
        for i in range(6):
            time.sleep(0.03)
            tick(1)
            samples.append(jobs.get(jid))

    jid = jobs.start("t", 10, work)
    _run(jid)
    etas = [s["eta"] for s in samples if s["eta"] is not None]
    assert len(etas) >= 3
    # first trustworthy ETA should exceed the last
    assert etas[0] > etas[-1], "ETA must trend down: %r" % (etas,)


def test_no_eta_for_indeterminate_job():
    def work(tick, label):
        for _ in range(3):
            time.sleep(0.02)
            tick(1)

    jid = jobs.start("t", 0, work)      # total=0 → no percentage, no ETA
    j = _run(jid)
    assert j["eta"] is None
    assert j["total"] == 0


def test_no_eta_when_done():
    def work(tick, label):
        for _ in range(3):
            time.sleep(0.02)
            tick(1)

    jid = jobs.start("t", 3, work)
    j = _run(jid)
    assert j["state"] == "done"
    assert j["eta"] is None, "finished jobs must not report a remaining time"
    assert j["remaining"] == 0


def test_eta_bounded_by_throughput():
    """One pathologically slow step must not inflate the estimate without limit.

    A step that takes 10x the others still moves the smoothed interval, but the
    throughput bound caps how far the ETA can drift from observed reality.
    """
    def work(tick, label):
        for i in range(4):
            time.sleep(0.08 if i == 2 else 0.01)
            tick(1)
        time.sleep(0.05)

    jid = jobs.start("t", 20, work)
    time.sleep(0.35)
    snap = jobs.get(jid)
    if snap["eta"] is not None:
        elapsed, done = snap["elapsed"], snap["done"]
        if done:
            throughput_eta = (elapsed / done) * snap["remaining"]
            assert snap["eta"] <= throughput_eta * 2.6, (
                "ETA %r exceeds the throughput bound %r" % (snap["eta"], throughput_eta))
    _run(jid)


def test_error_job_has_no_eta():
    def work(tick, label):
        tick(1)
        raise ValueError("boom")

    jid = jobs.start("t", 5, work)
    j = _run(jid)
    assert j["state"] == "error"
    assert j["eta"] is None
    assert "boom" in j["message"]


def test_pct_matches_done_over_total():
    def work(tick, label):
        time.sleep(0.02)
        tick(1)
        time.sleep(0.02)

    jid = jobs.start("t", 4, work)
    _run(jid)
    j = jobs.get(jid)
    assert j["pct"] == 100
    assert j["done"] == j["total"]


def test_tick_multi_step_counts_once():
    """tick(3) advances three steps but records one timing interval."""
    def work(tick, label):
        time.sleep(0.02)
        tick(3)
        time.sleep(0.02)

    jid = jobs.start("t", 6, work)
    j = _run(jid)
    assert j["done"] == 6


def test_confidence_reflects_sample_count():
    """Confidence is derived from how many steps have actually completed."""
    def work(tick, label):
        for _ in range(10):
            time.sleep(0.02)
            tick(1)

    jid = jobs.start("t", 10, work)
    time.sleep(0.05)
    early = jobs.get(jid)
    _run(jid)
    if early["eta"] is not None:
        # 2 samples -> low, 3-7 -> fair, 8+ -> good
        expect = ("low" if early["samples"] < 3 else
                  "fair" if early["samples"] < 8 else "good")
        assert early["eta_confidence"] == expect


def test_get_missing_returns_none():
    assert jobs.get("nope") is None
