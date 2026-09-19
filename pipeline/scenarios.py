"""A4: scenario generation (SPEC.md §2 scenario table, §4 A4).

Invariant: `ground_truth` is ALWAYS the position's label, never derived from `kind`.
You can blunder in a position that deserved a fast move; that blunder scenario's correct
answer is SHORT. GRAY positions never become scenarios.

Idempotent: the desired set of (position_id, kind) is recomputed each run. Existing rows
keep their ids (drill_attempts point at them); rows that no longer qualify are deleted
unless they have attempts, in which case they are kept and reported.
"""
from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import groupby
from typing import Callable

from pipeline import config
from pipeline.metrics import is_fork, percentile

Log = Callable[[str], None]

Key = tuple[int, str]                       # (position_id, kind)
Value = tuple[str, float | None, dict]      # (ground_truth, severity, notes)


def _tc(r: sqlite3.Row) -> str:
    return f"{r['base_seconds']}+{r['increment']}"


def _bucket(r: sqlite3.Row) -> tuple[int, int]:
    clock = r["clock_before"]
    return (r["ply"] // config.CALM_PLY_BUCKET,
            -1 if clock is None else int(clock // config.CALM_CLOCK_BUCKET_S))


def load_rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT p.id, p.game_id, p.ply, p.user_to_move, p.label, p.e_best, p.e_played, p.e_loss,
                  p.seconds_spent, p.time_fraction, p.clock_before, p.commitment,
                  g.base_seconds, g.increment, g.result_user
             FROM positions p JOIN games g ON g.id = p.game_id
            WHERE p.label IS NOT NULL
            ORDER BY p.game_id, p.ply""").fetchall()


def desired_scenarios(rows: list[sqlite3.Row], log: Log = print) -> dict[Key, Value]:
    """Pure function: analyzed positions -> {(position_id, kind): (truth, severity, notes)}."""
    user_rows = [r for r in rows if r["user_to_move"]]
    want: dict[Key, Value] = {}

    # too_little needs the user's own pace per time control.
    by_tc: dict[str, list[float]] = defaultdict(list)
    for r in user_rows:
        if r["seconds_spent"] is not None:
            by_tc[_tc(r)].append(r["seconds_spent"])
    p_fast = {tc: percentile(v, config.TOO_LITTLE_PERCENTILE) for tc, v in by_tc.items()}

    for r in user_rows:
        if r["label"] == "GRAY":
            continue
        truth, loss = r["label"], r["e_loss"]
        if loss is not None and loss >= config.BLUNDER_E_LOSS:
            want[(r["id"], "blunder")] = (truth, loss, {})
        fast = p_fast.get(_tc(r))
        if (truth == "LONG" and loss is not None and loss >= config.TOO_LITTLE_E_LOSS
                and r["seconds_spent"] is not None and fast is not None and r["seconds_spent"] < fast):
            want[(r["id"], "too_little")] = (truth, loss, {"p25_seconds": fast})
        if (truth == "SHORT" and r["time_fraction"] is not None
                and r["time_fraction"] >= config.TOO_MUCH_TIME_FRACTION):
            want[(r["id"], "too_much")] = (truth, loss, {"time_fraction": r["time_fraction"]})
        if is_fork(r["commitment"]):
            want[(r["id"], "fork")] = (truth, r["commitment"], {})

    # lost_advantage: user's E peaked >= 0.75 in a game not won; the user move with the
    # largest drop after the peak (non-GRAY so it can be drilled).
    for game_id, group in groupby(rows, key=lambda r: r["game_id"]):
        g = list(group)
        if g[0]["result_user"] > config.LOST_ADVANTAGE_MAX_RESULT:
            continue
        e_user = [r["e_best"] if r["user_to_move"] else 1.0 - r["e_best"] for r in g]
        peak_i = max(range(len(g)), key=lambda i: e_user[i])
        if e_user[peak_i] < config.LOST_ADVANTAGE_PEAK_E:
            continue
        cands = [r for r in g[peak_i:] if r["user_to_move"] and r["label"] != "GRAY"
                 and r["e_loss"] is not None and r["e_loss"] > 0]
        if not cands:
            continue
        worst = max(cands, key=lambda r: r["e_loss"])
        want[(worst["id"], "lost_advantage")] = (
            worst["label"], worst["e_loss"],
            {"peak_e": round(e_user[peak_i], 4), "peak_ply": g[peak_i]["ply"]})

    # calm: SHORT positions sampled to match the LONG scenarios' (ply, clock) buckets,
    # so the drill cannot be solved by reading the clock. Target LONG:SHORT ~ 50:50.
    truth_of: dict[int, str] = {pid: v[0] for (pid, _), v in want.items()}
    n_long = sum(t == "LONG" for t in truth_of.values())
    n_short = sum(t == "SHORT" for t in truth_of.values())
    n_calm = max(config.CALM_MIN, n_long - n_short)
    pool = [r for r in user_rows if r["label"] == "SHORT" and r["id"] not in truth_of]
    pool.sort(key=lambda r: r["id"])
    rng = random.Random(config.CALM_SEED)
    by_bucket: dict[tuple[int, int], list] = defaultdict(list)
    for r in pool:
        by_bucket[_bucket(r)].append(r)
    long_buckets = Counter(_bucket(r) for r in user_rows if r["id"] in truth_of and truth_of[r["id"]] == "LONG")
    chosen: list = []
    for b, n in sorted(long_buckets.items()):
        k = min(len(by_bucket[b]), round(n_calm * n / max(1, n_long)))
        picked = rng.sample(by_bucket[b], k)
        chosen.extend(picked)
        by_bucket[b] = [r for r in by_bucket[b] if r not in picked]
    leftover = sorted((r for rs in by_bucket.values() for r in rs), key=lambda r: r["id"])
    if len(chosen) < n_calm and leftover:
        chosen.extend(rng.sample(leftover, min(n_calm - len(chosen), len(leftover))))
    for r in chosen:
        b = _bucket(r)
        want[(r["id"], "calm")] = ("SHORT", r["e_loss"], {"ply_bucket": b[0], "clock_bucket": b[1]})

    log(f"[scenarios] user positions={len(user_rows)} long_scenarios={n_long} "
        f"short_scenarios={n_short} calm_added={len(chosen)}")
    return want


def write_scenarios(con: sqlite3.Connection, want: dict[Key, Value], log: Log = print) -> Counter:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    existing = {(r["position_id"], r["kind"]): r["id"]
                for r in con.execute("SELECT id, position_id, kind FROM scenarios")}
    kept_stale = 0
    for key, sid in existing.items():
        if key in want:
            continue
        attempts = con.execute("SELECT COUNT(*) FROM drill_attempts WHERE scenario_id = ?",
                               (sid,)).fetchone()[0]
        if attempts:
            kept_stale += 1
        else:
            con.execute("DELETE FROM scenarios WHERE id = ?", (sid,))
    for (pid, kind), (truth, severity, notes) in sorted(want.items()):
        notes_json = json.dumps(notes, separators=(",", ":"))
        if (pid, kind) in existing:
            con.execute("UPDATE scenarios SET ground_truth = ?, severity = ?, notes_json = ? WHERE id = ?",
                        (truth, severity, notes_json, existing[(pid, kind)]))
        else:
            con.execute("""INSERT INTO scenarios (position_id, kind, ground_truth, severity, notes_json, created_at)
                           VALUES (?,?,?,?,?,?)""", (pid, kind, truth, severity, notes_json, now))
    con.commit()
    counts = Counter(con.execute("SELECT kind, COUNT(*) FROM scenarios GROUP BY kind").fetchall())
    if kept_stale:
        log(f"[scenarios] kept {kept_stale} stale scenario(s) that already have drill attempts")
    return counts


def generate(con: sqlite3.Connection, log: Log = print) -> Counter:
    want = desired_scenarios(load_rows(con), log=log)
    counts = write_scenarios(con, want, log=log)
    truths = dict(con.execute("SELECT ground_truth, COUNT(*) FROM scenarios GROUP BY 1").fetchall())
    log(f"[scenarios] total={sum(truths.values())} by truth={truths} by kind="
        f"{dict(sorted((k, n) for (k, n) in counts))}")
    return counts
