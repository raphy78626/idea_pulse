"""Queue MiroFish reaction simulations for BUILD ideas, slowly.

Why a separate queue rather than run_simulations()
--------------------------------------------------
`app.run_simulations()` exists for cron but has two problems for this job:

  1. `SIMULATABLE_SOURCES` whites the whitelist down to three "problem" sources,
     which leaves only 1 of 15 BUILD ideas eligible. Measured seed expansion on
     a supposedly-ineligible Fazier idea produced 9 named actors, so the
     restriction was protecting nothing — seed expansion already decides quality.
  2. It has no pacing. Each run costs 6-11 minutes of model calls and holds the
     single Kuzu graph handle, so firing them back to back starves everything
     else on the machine.

This script therefore:
  - selects any item with verdict='BUILD' (highest buildability first)
  - runs at most MAX_PER_NIGHT, with a configurable gap between runs
  - respects the attempt cap and never redoes a finished report
  - exits cleanly if MiroFish is down rather than recording a false failure
  - is safe to interrupt: state lives in the DB, so re-running resumes

Usage
-----
    # queue every BUILD idea, 90s apart, 6 per run
    python sim_queue.py

    # a longer soak: 12 runs, 5 min apart
    python sim_queue.py --max 12 --gap 300

    # see what would happen without running anything
    python sim_queue.py --dry-run

Designed to be launched under tmux so it survives the shell:
    tmux new-session -d -s simq \
      "cd .../idea_pulse && python -u sim_queue.py > /tmp/sim_queue.log 2>&1"
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import mirofish_bridge
from app import EVAL_DIR, db

# Re-simulating a hopeless idea wastes 10 minutes; two attempts is enough.
MAX_ATTEMPTS = 2


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def eligible(include_all_sources=True):
    """BUILD ideas that still need a reaction report, best first."""
    conn = db()
    conn.row_factory = sqlite3.Row
    where = ["verdict = 'BUILD'",
             "COALESCE(sim_attempts, 0) < ?",
             "(sim_path IS NULL OR sim_path = '')"]
    if not include_all_sources:
        # the old whitelist, kept for comparison only
        where.append("source IN ('Reddit business ideas','Ask HN ideas','SomebodyMakeThis')")
    rows = conn.execute(
        f"""SELECT id, title, meta, source, buildability, sim_attempts
            FROM items WHERE {' AND '.join(where)}
            ORDER BY COALESCE(buildability,0) DESC, COALESCE(score,0) DESC""",
        (MAX_ATTEMPTS,)).fetchall()
    conn.close()
    return rows


def run_one(row):
    """Simulate one idea. Returns (ok, detail)."""
    item_id = row["id"]
    out_path = EVAL_DIR / f"sim_{item_id}.md"
    EVAL_DIR.mkdir(parents=True, exist_ok=True)

    evaluation_md = ""
    try:
        conn = db()
        found = conn.execute("SELECT report_path FROM items WHERE id=?", (item_id,)).fetchone()
        conn.close()
        if found and found[0]:
            evaluation_md = Path(found[0]).read_text()[:1500]
    except OSError:
        pass

    try:
        result = mirofish_bridge.run_simulation(
            row["title"], row["meta"] or "", row["source"],
            out_path=out_path, evaluation=evaluation_md)
        text = mirofish_bridge.read_report(result.get("report_path") or str(out_path))
        if not text:
            raise mirofish_bridge.BridgeError("report was empty or cut off")
        conn = db()
        conn.execute("""UPDATE items SET sim_path=?,
                        sim_attempts=COALESCE(sim_attempts,0)+1, sim_error=NULL
                        WHERE id=?""", (str(out_path), item_id))
        conn.commit()
        conn.close()
        return True, f"report {len(text)} chars -> {out_path.name}"
    except Exception as exc:
        detail = str(exc)[:240]
        conn = db()
        conn.execute("""UPDATE items SET sim_attempts=COALESCE(sim_attempts,0)+1,
                        sim_error=? WHERE id=?""", (detail, item_id))
        conn.commit()
        conn.close()
        return False, detail


def main():
    ap = argparse.ArgumentParser(description="Slow MiroFish queue for BUILD ideas")
    ap.add_argument("--max", type=int, default=6, help="max simulations this run")
    ap.add_argument("--gap", type=int, default=90,
                    help="seconds to wait between simulations")
    ap.add_argument("--dry-run", action="store_true", help="list the queue, run nothing")
    args = ap.parse_args()

    rows = eligible()
    log(f"queue: {len(rows)} BUILD ideas need a simulation")

    if args.dry_run:
        for r in rows[:args.max]:
            log(f"  would run #{r['id']} [{r['source'][:20]}] "
                f"build={r['buildability']} {r['title'][:52]}")
        return 0

    if not rows:
        log("nothing to do")
        return 0

    ok, hint = mirofish_bridge.preflight()
    if not ok:
        # Do NOT record an attempt: the failure is environmental, not the idea's.
        log(f"MiroFish unavailable — stopping without touching the queue. {hint}")
        return 2

    done = failed = 0
    for i, row in enumerate(rows[:args.max], 1):
        log(f"[{i}/{min(args.max, len(rows))}] #{row['id']} "
            f"[{row['source'][:20]}] {row['title'][:54]}")
        started = time.time()
        success, detail = run_one(row)
        elapsed = time.time() - started
        if success:
            done += 1
            log(f"    ✓ {detail} ({elapsed:.0f}s)")
        else:
            failed += 1
            log(f"    ✗ {detail} ({elapsed:.0f}s)")

        if i < min(args.max, len(rows)):
            log(f"    resting {args.gap}s before the next one")
            time.sleep(args.gap)

    log(f"finished: {done} simulated, {failed} failed")
    remaining = len(eligible())
    log(f"{remaining} BUILD ideas still queued for a future run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
