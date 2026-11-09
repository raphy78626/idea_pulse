"""Drain the first-currency queue in the background.

Usage:
    python eval_money_batch.py            # keep going until nothing is pending
    python eval_money_batch.py 20         # stop after ~20 items

Runs in a tmux session so it survives the shell that launched it:

    tmux new-session -d -s ip-money \
      "cd .../idea_pulse && python -u eval_money_batch.py > /tmp/money_bg.log 2>&1"

Each item takes ~11-20s on free models, so the full queue is a coffee break,
not a hang. Progress is written to stdout and to the DB as it goes.
"""
import sys
import time

import app

SOURCES = ("Reddit business ideas", "HN demand signals",
           "Product Hunt", "Fazier", "BuildHop", "Awesome Indie",
           "OpenHunts", "Uneed")


def pending():
    conn = app.db()
    marks = ",".join("?" * len(SOURCES))
    n = conn.execute(
        f"""SELECT COUNT(*) FROM items WHERE source IN ({marks})
            AND (earn_verdict IS NULL OR earn_verdict='ERROR')""",
        SOURCES).fetchone()[0]
    conn.close()
    return n


def main(target=None):
    start = time.time()
    done = 0
    round_no = 0
    while True:
        left = pending()
        round_no += 1
        print(f"--- round {round_no}: {left} pending, {done} done so far", flush=True)
        if left == 0:
            print("queue empty", flush=True)
            break
        if target and done >= target:
            print(f"reached target {target}", flush=True)
            break
        batch = 5 if not target else min(5, target - done)
        n, used = app.evaluate_money(limit=batch, sources=SOURCES)
        if n == 0:
            print("no progress this round (models failing?) — stopping", flush=True)
            break
        done += n
        print(f"    evaluated {n} | models {used} | total {done} | "
              f"{time.time() - start:.0f}s elapsed", flush=True)
    print(f"DONE {done} items in {time.time() - start:.0f}s", flush=True)

    # summary by verdict
    conn = app.db()
    marks = ",".join("?" * len(SOURCES))
    rows = conn.execute(
        f"""SELECT earn_verdict, COUNT(*) FROM items WHERE source IN ({marks})
            GROUP BY earn_verdict ORDER BY COUNT(*) DESC""", SOURCES).fetchall()
    conn.close()
    print("\nverdict summary:")
    for v, c in rows:
        print(f"  {v or '(pending)':<10} {c}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
