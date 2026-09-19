"""Drive the Bucket B drill API against a real pipeline database.

`tests/test_app.py` runs on a tiny hand-built fixture. This is the integration check
that nobody had run: point the app at a DB the pipeline actually produced and walk the
whole loop -- deal a position, answer it, deal the next, read the stats page.

    TUTOR_DB=data/_probe.db .venv/Scripts/python tools/smoke_app.py

Exits non-zero on the first problem. Writes drill_attempts, so use a throwaway copy.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import db as appdb  # noqa: E402
from app.main import app  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILURES.append(msg)


def main() -> int:
    path = os.environ.get("TUTOR_DB", "data/_probe.db")
    if not Path(path).exists():
        print(f"no such DB: {path}")
        return 2
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    appdb.set_connection(con) if hasattr(appdb, "set_connection") else None
    from app import main as appmain
    appmain.set_connection(con)
    client = TestClient(app)

    total = con.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0]
    print(f"DB {path}: {total} scenarios")

    print("\n[1] deal positions and answer them")
    seen: set[int] = set()
    truths = {"LONG": 0, "SHORT": 0}
    correct_reported = 0
    for i in range(25):
        r = client.get("/api/drill/next", params={"exclude": ",".join(map(str, seen))})
        if r.status_code != 200:
            check(False, f"drill/next returned {r.status_code}: {r.text[:200]}")
            break
        d = r.json()
        if d.get("done") or d.get("scenario_id") is None:
            check(i > 0, f"ran out of scenarios after {i} deals (have {total})")
            break
        sid = d["scenario_id"]
        if sid in seen:
            check(False, f"deal {i+1}: scenario {sid} dealt twice")
        seen.add(sid)
        if "fen" not in d:
            check(False, f"deal {i+1}: response missing 'fen'; keys={sorted(d)}")
        # The deal must NOT leak the answer: it is a quiz. Read truth from the DB instead.
        if i == 0:
            check("ground_truth" not in d and "label" not in d,
                  f"deal payload withholds the answer; keys={sorted(d)}")
        truth = con.execute("SELECT ground_truth FROM scenarios WHERE id = ?", (sid,)).fetchone()[0]
        if truth in truths:
            truths[truth] += 1
        # Answer every position LONG; the API decides correctness.
        a = client.post("/api/drill/answer", json={"scenario_id": sid, "answer": "LONG",
                                                   "response_ms": 1200})
        if a.status_code != 200:
            check(False, f"drill/answer returned {a.status_code}: {a.text[:200]}")
            break
        body = a.json()
        if body.get("correct"):
            correct_reported += 1
        if i == 0:
            print(f"    first answer payload keys: {sorted(body)}")
    check(len(seen) >= 20, f"dealt {len(seen)} distinct scenarios without repeating")
    print(f"    ground truth seen: {truths}, API said correct {correct_reported}x "
          f"(we always answered LONG)")
    check(truths["LONG"] == correct_reported,
          f"'correct' matches ground truth ({truths['LONG']} LONG dealt, "
          f"{correct_reported} marked correct)")

    print("\n[2] the exclude list is honoured at scale")
    r = client.get("/api/drill/next", params={"exclude": ",".join(map(str, seen))})
    check(r.status_code == 200, f"drill/next with {len(seen)} exclusions -> {r.status_code}")
    if r.status_code == 200 and not r.json().get("done"):
        check(r.json().get("scenario_id") not in seen, "still excludes everything already seen")

    print("\n[3] attempts were persisted")
    n = con.execute("SELECT COUNT(*) FROM drill_attempts").fetchone()[0]
    check(n == len(seen), f"drill_attempts rows = {n}, expected {len(seen)}")

    print("\n[4] stats")
    r = client.get("/api/stats")
    check(r.status_code == 200, f"/api/stats -> {r.status_code}")
    if r.status_code == 200:
        s = r.json()
        print(f"    {s}"[:300])
        check(bool(s), "stats payload is not empty")

    print("\n[5] pages render")
    for route in ("/", "/stats"):
        r = client.get(route)
        check(r.status_code == 200 and len(r.text) > 200, f"{route} -> {r.status_code}")

    print("\n[6] scenario detail (the reveal panel)")
    sid = next(iter(seen), None)
    if sid is not None:
        r = client.get(f"/api/scenarios/{sid}")
        check(r.status_code == 200, f"/api/scenarios/{sid} -> {r.status_code}")
        if r.status_code == 200:
            print(f"    keys: {sorted(r.json())}")

    print("\n[7] how hard is the drill actually?")
    mix = dict(con.execute("SELECT ground_truth, COUNT(*) FROM scenarios GROUP BY 1").fetchall())
    n = sum(mix.values())
    lazy = max(mix.values()) / n if n else 0
    print(f"    scenario mix {mix}")
    print(f"    always answering '{max(mix, key=mix.get)}' scores {100*lazy:.0f}% without looking")
    check(lazy <= 0.60, f"a one-word strategy scores {100*lazy:.0f}%, target is near 50%")

    print("\n" + ("FAILURES:\n  " + "\n  ".join(FAILURES) if FAILURES else "all checks passed"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
