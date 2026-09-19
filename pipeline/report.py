"""A5: the tuning report. Everything we need to retune config.py after a real run."""
from __future__ import annotations

import sqlite3
from typing import Callable

from pipeline import config
from pipeline.db import count

Log = Callable[[str], None]


def _bar(n: int, total: int, width: int = 30) -> str:
    return "#" * (0 if not total else round(width * n / total))


def histogram(values: list[float], edges: list[float]) -> list[tuple[str, int]]:
    """Counts per [lo, hi) bin; the last bin includes its upper edge."""
    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        last = i == len(edges) - 2
        n = sum(1 for v in values if lo <= v < hi or (last and v == hi))
        out.append((f"[{lo:.3f}, {hi:.3f}{']' if last else ')'}", n))
    return out


def report(con: sqlite3.Connection, out: Log = print) -> None:
    games = count(con, "SELECT COUNT(*) FROM games")
    analyzed_games = count(con, "SELECT COUNT(*) FROM games WHERE analyzed_at IS NOT NULL")
    positions = count(con, "SELECT COUNT(*) FROM positions")
    scored = count(con, "SELECT COUNT(*) FROM positions WHERE label IS NOT NULL")
    user_scored = count(con, "SELECT COUNT(*) FROM positions WHERE label IS NOT NULL AND user_to_move = 1")
    analysis_rows = count(con, "SELECT COUNT(*) FROM analysis")
    depths = [r[0] for r in con.execute("SELECT DISTINCT analysis_depth FROM games WHERE analysis_depth IS NOT NULL")]

    out("=== Chess Effort Tutor: pipeline report ===")
    out(f"games: {games} (analyzed {analyzed_games}, depth(s) {depths})   "
        f"positions: {positions} (scored {scored}, user-to-move {user_scored})   "
        f"analysis cache rows: {analysis_rows}")
    tcs = con.execute("""SELECT base_seconds, increment, COUNT(*), ROUND(AVG(result_user), 3)
                           FROM games GROUP BY 1, 2 ORDER BY 3 DESC""").fetchall()
    out("time controls: " + ", ".join(f"{b}+{i}: {n} games (score {s})" for b, i, n, s in tcs))
    out(f"thresholds: CRIT={config.CRIT} CALM={config.CALM} FORK={config.FORK} "
        f"SHALLOW_DEPTH={config.SHALLOW_DEPTH} ANALYSIS_DEPTH={config.ANALYSIS_DEPTH}")
    if not user_scored:
        out("no scored positions yet: run `python -m pipeline analyze`")
        return

    crits = [r[0] for r in con.execute(
        "SELECT criticality FROM positions WHERE criticality IS NOT NULL AND user_to_move = 1")]
    out("")
    out(f"criticality histogram (user-to-move, n={len(crits)}; edges include CALM and CRIT):")
    for lab, n in histogram(crits, config.CRIT_HIST_EDGES):
        out(f"  {lab:<18} {n:5d}  {_bar(n, len(crits))}")

    out("")
    out("labels (user-to-move):")
    for lab, n, obv in con.execute("""SELECT label, COUNT(*), ROUND(AVG(obvious), 3) FROM positions
                                       WHERE label IS NOT NULL AND user_to_move = 1
                                       GROUP BY label ORDER BY label"""):
        out(f"  {lab:<6} {n:5d}  ({100 * n / user_scored:4.1f}%)  obvious rate {obv}")
    obvious_rate = con.execute("SELECT ROUND(AVG(obvious), 3) FROM positions "
                               "WHERE obvious IS NOT NULL AND user_to_move = 1").fetchone()[0]
    out(f"  obvious overall: {obvious_rate}")

    out("")
    out("scenarios by kind (ground_truth LONG / SHORT):")
    rows = con.execute("""SELECT kind, SUM(ground_truth = 'LONG'), SUM(ground_truth = 'SHORT'), COUNT(*)
                            FROM scenarios GROUP BY kind ORDER BY kind""").fetchall()
    for kind, n_long, n_short, n in rows:
        out(f"  {kind:<15} {n:4d}   LONG {n_long:4d}   SHORT {n_short:4d}")
    truths = dict(con.execute("SELECT ground_truth, COUNT(*) FROM scenarios GROUP BY 1").fetchall())
    out(f"  total {sum(truths.values())}: {truths}")

    out("")
    out("per-phase (user-to-move): mean e_loss, mean criticality, mean seconds spent:")
    for ph, n, loss, crit, spent in con.execute(
            """SELECT phase, COUNT(*), ROUND(AVG(e_loss), 4), ROUND(AVG(criticality), 4),
                      ROUND(AVG(seconds_spent), 1)
                 FROM positions WHERE label IS NOT NULL AND user_to_move = 1
                GROUP BY phase ORDER BY CASE phase WHEN 'opening' THEN 0 WHEN 'middlegame' THEN 1 ELSE 2 END"""):
        out(f"  {ph:<11} n={n:5d}  e_loss {loss}  criticality {crit}  seconds {spent}")

    out("")
    out("seconds spent by label (user-to-move): mean / median-ish (p50):")
    for lab, in con.execute("SELECT DISTINCT label FROM positions WHERE label IS NOT NULL ORDER BY label"):
        vals = sorted(r[0] for r in con.execute(
            "SELECT seconds_spent FROM positions WHERE label = ? AND user_to_move = 1 "
            "AND seconds_spent IS NOT NULL", (lab,)))
        if vals:
            out(f"  {lab:<6} mean {sum(vals) / len(vals):5.1f}s  p50 {vals[len(vals) // 2]:5.1f}s  n={len(vals)}")
