"""One-shot: evaluate items from the newly scraped launch directories.

The dashboard's normal /evaluate orders by score across all pending items, so the
new sources would sit behind thousands of older ones forever. This targets them.
"""
import sqlite3
import sys

import app

SOURCES = ("Fazier", "BuildHop", "Awesome Indie", "OpenHunts", "Uneed", "Product Hunt")


def main(limit=20):
    conn = app.db()
    conn.row_factory = sqlite3.Row
    marks = ",".join("?" * len(SOURCES))
    rows = conn.execute(
        f"""SELECT id, title, meta, source FROM items
            WHERE source IN ({marks}) AND (verdict IS NULL OR verdict='ERROR')
            ORDER BY score DESC, eng DESC LIMIT ?""",
        (*SOURCES, limit)).fetchall()
    conn.close()
    print(f"targets: {len(rows)}", flush=True)

    used = {}
    for r in rows:
        d, model = app._eval_call(r["title"], r["meta"])
        used[model] = used.get(model, 0) + 1
        c = app.db()
        c.execute("""UPDATE items SET verdict=?, buildability=?, ttm_days=?, what=?, reason=?
                     WHERE id=?""",
                  (d["verdict"], d["buildability"], d["ttm_days"],
                   d.get("what", "")[:200], d.get("reason", "")[:200], r["id"]))
        c.commit()
        c.close()
        print(f"  {d['verdict']:<5} {d['buildability']:>2}/10  {r['source']:<14} "
              f"{r['title'][:44]}", flush=True)

    print(f"DONE models={used}", flush=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
