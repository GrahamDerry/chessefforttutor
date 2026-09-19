"""Read-only threshold sweep for the joint config.py retune (SPEC.md §6 M2).

Answers, from whatever is already analyzed in TUTOR_DB:
  1. how much the `obvious` short-circuit costs us in LONG labels,
  2. what the label mix would be at other CRIT / CALM values,
  3. what the drillable scenario mix looks like at each of those settings.

Nothing here writes to the DB and nothing here runs the engine, so it is safe to run
while `pipeline analyze` is still going. Numbers move as more games land.

    .venv/Scripts/python tools/tune_sweep.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import config, db  # noqa: E402


def label_at(crit: float | None, obvious: bool, fork: bool, crit_t: float, calm_t: float) -> str:
    """pipeline.metrics.label with the thresholds passed in instead of read from config."""
    if fork:
        return "LONG"
    if obvious or crit is None or crit <= calm_t:
        return "SHORT"
    if crit >= crit_t:
        return "LONG"
    return "GRAY"


def main() -> int:
    con = db.connect(os.environ.get("TUTOR_DB", "data/tutor.db"))
    con.execute("PRAGMA busy_timeout = 60000")
    rows = con.execute(
        """SELECT p.criticality, p.obvious, p.commitment, p.e_loss, p.seconds_spent
             FROM positions p JOIN games g ON g.id = p.game_id
            WHERE p.user_to_move = 1 AND p.criticality IS NOT NULL
              AND g.analysis_depth IS NOT NULL"""
    ).fetchall()
    n = len(rows)
    if not n:
        print("nothing analyzed yet")
        return 1

    forks = [r for r in rows if r["commitment"] is not None and r["commitment"] >= config.FORK]
    obv = [r for r in rows if r["obvious"]]
    print(f"analyzed user-to-move positions: {n}")
    print(f"  obvious (shallow depth {config.SHALLOW_DEPTH} agrees with depth {config.ANALYSIS_DEPTH}): "
          f"{len(obv)} ({100*len(obv)/n:.1f}%)")
    print(f"  fork pass has run on: {sum(1 for r in rows if r['commitment'] is not None)}; "
          f"forks found: {len(forks)}")

    # 1. What `obvious` costs. These are positions the engine says are sharp but that we
    #    label SHORT anyway because a depth-4 probe already found the best move.
    print("\n=== cost of the `obvious` short-circuit ===")
    for t in (config.CRIT, 0.15, 0.20, 0.30):
        sharp = [r for r in rows if r["criticality"] >= t]
        lost = [r for r in sharp if r["obvious"]]
        if sharp:
            print(f"  criticality >= {t:.2f}: {len(sharp):4d} positions, "
                  f"{len(lost):4d} ({100*len(lost)/len(sharp):.0f}%) forced to SHORT by `obvious`")
    hi = [r for r in rows if r["criticality"] >= config.CRIT and r["obvious"]]
    if hi:
        big = [r for r in hi if (r["e_loss"] or 0) >= config.BLUNDER_E_LOSS]
        print(f"  of those forced-SHORT sharp positions, {len(big)} are ones the user actually "
              f"blundered (e_loss >= {config.BLUNDER_E_LOSS})")

    # 2. Label mix across candidate thresholds.
    print("\n=== label mix by (CALM, CRIT) ===")
    print(f"  {'CALM':>5} {'CRIT':>5} | {'LONG':>6} {'GRAY':>6} {'SHORT':>6} | LONG share  drillable LONG:SHORT")
    for calm_t in (0.02, config.CALM, 0.05):
        for crit_t in (0.06, 0.08, config.CRIT, 0.15):
            if crit_t <= calm_t:
                continue
            c = Counter(label_at(r["criticality"], bool(r["obvious"]),
                                 r["commitment"] is not None and r["commitment"] >= config.FORK,
                                 crit_t, calm_t) for r in rows)
            long_n, short_n = c["LONG"], c["SHORT"]
            ratio = f"1:{short_n/long_n:.0f}" if long_n else "1:inf"
            star = "  <- current" if (calm_t == config.CALM and crit_t == config.CRIT) else ""
            print(f"  {calm_t:5.2f} {crit_t:5.2f} | {long_n:6d} {c['GRAY']:6d} {short_n:6d} | "
                  f"{100*long_n/n:8.1f}%  {ratio:>16}{star}")

    # 3. Same sweep, but counting only positions a drill could actually use: the calm
    #    sampler matches SHORT to LONG, so LONG supply is what caps the scenario count.
    print("\n=== what LONG supply means for the drill ===")
    for crit_t in (0.06, 0.08, config.CRIT, 0.15):
        long_rows = [r for r in rows
                     if not r["obvious"] and r["criticality"] is not None
                     and r["criticality"] > config.CALM and r["criticality"] >= crit_t]
        per_game = len(long_rows) / max(1, len({id(r) for r in rows})) if rows else 0
        print(f"  CRIT={crit_t:.2f}: {len(long_rows):4d} LONG positions available "
              f"-> ~{2*len(long_rows):4d} scenarios at a 50:50 mix")
    print(f"\n  (spec target is LONG:SHORT ~ 50:50 in `scenarios`; CALM_MIN={config.CALM_MIN} "
          f"floors the calm sample, which is what skews a small DB toward SHORT)")

    # 4. Does `obvious` actually predict THIS user's play? The flag means a depth-4 probe
    #    found the same move as depth 18. It is only a fair stand-in for "I would have
    #    found this at a glance" if the user in fact plays these positions well.
    print("\n=== does `obvious` predict the user's own accuracy? ===")
    val = con.execute(
        """SELECT p.obvious, p.criticality, p.e_loss, p.move_played = a.best_move AS matched
             FROM positions p
             JOIN games g ON g.id = p.game_id
             JOIN analysis a ON a.id = p.analysis_id
            WHERE p.user_to_move = 1 AND p.criticality IS NOT NULL AND p.e_loss IS NOT NULL"""
    ).fetchall()
    print(f"  {'group':<28} {'n':>5} {'plays best':>11} {'mean e_loss':>12} {'blunder rate':>13}")
    groups = [
        ("obvious", [r for r in val if r["obvious"]]),
        ("not obvious", [r for r in val if not r["obvious"]]),
        (f"obvious & crit >= {config.CRIT}", [r for r in val if r["obvious"] and r["criticality"] >= config.CRIT]),
        (f"not obvious & crit >= {config.CRIT}", [r for r in val if not r["obvious"] and r["criticality"] >= config.CRIT]),
    ]
    for name, g in groups:
        if not g:
            continue
        best = sum(1 for r in g if r["matched"]) / len(g)
        loss = sum(r["e_loss"] for r in g) / len(g)
        blund = sum(1 for r in g if r["e_loss"] >= config.BLUNDER_E_LOSS) / len(g)
        print(f"  {name:<28} {len(g):5d} {100*best:10.1f}% {loss:12.4f} {100*blund:12.1f}%")
    print("  If the two `crit >=` rows look alike, a depth-%d probe is not modelling this"
          % config.SHALLOW_DEPTH)
    print("  player's intuition, and `obvious` is silencing positions they get wrong.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
