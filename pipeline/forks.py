"""A3: commitment / fork pass (SPEC.md §2 "Commitment", §4 A3).

Gate (position must satisfy all):
    * user to move, scored, criticality <= CALM
    * >= 2 candidates within NEAR_EQUAL_E of the best
    * at least one near-equal candidate is irreversible: pawn move, capture, castling,
      or its PV opens with a trade (capture immediately recaptured on the same square)

For each near-equal candidate, follow its PV FORK_PV_PLIES plies and compute criticality
(MultiPV=4 at FORK_DEPTH) at each of the user's decision points along the way.
    sharpness(m) = mean(C at those points)
    commitment   = max(sharpness) - min(sharpness)
    fork         = commitment >= FORK  -> label upgraded to LONG

Idempotent: only positions with commitment IS NULL are visited unless `recompute=True`.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Callable, Sequence

import chess

from pipeline import config
from pipeline.engine import AnalysisCache, Engine, analyze_position
from pipeline.metrics import criticality, label

Log = Callable[[str], None]


# --------------------------------------------------------------------------- pure parts
def near_equal(candidates: Sequence[dict]) -> list[dict]:
    """Candidates within NEAR_EQUAL_E of the best (best included)."""
    if not candidates:
        return []
    top = float(candidates[0]["e"])
    return [c for c in candidates if top - float(c["e"]) <= config.NEAR_EQUAL_E]


def opens_with_trade(board: chess.Board, pv: Sequence[str]) -> bool:
    """First move captures and the reply recaptures on the same square."""
    if len(pv) < 2:
        return False
    m1, m2 = chess.Move.from_uci(pv[0]), chess.Move.from_uci(pv[1])
    if not board.is_capture(m1):
        return False
    b = board.copy(stack=False)
    b.push(m1)
    return m2 in b.legal_moves and b.is_capture(m2) and m2.to_square == m1.to_square


def is_irreversible(board: chess.Board, cand: dict) -> bool:
    move = chess.Move.from_uci(cand["move"])
    if move not in board.legal_moves:
        return False
    if board.is_capture(move) or board.is_castling(move):
        return True
    piece = board.piece_at(move.from_square)
    if piece is not None and piece.piece_type == chess.PAWN:
        return True
    return opens_with_trade(board, cand.get("pv") or [cand["move"]])


def passes_gate(board: chess.Board, crit: float | None, candidates: Sequence[dict]) -> list[dict]:
    """The near-equal candidates if the position is worth a commitment check, else []."""
    if crit is None or crit > config.CALM:
        return []
    ne = near_equal(candidates)
    if len(ne) < 2 or not any(is_irreversible(board, c) for c in ne):
        return []
    return ne


def user_decision_points(board: chess.Board, pv: Sequence[str], plies: int = config.FORK_PV_PLIES
                         ) -> list[chess.Board]:
    """Positions along `pv` (after 2, 4, ... plies) where the mover of `board` is to move again.

    Stops at the end of the PV or at a terminal position.
    """
    b = board.copy(stack=False)
    out: list[chess.Board] = []
    for i, uci in enumerate(pv[:plies], start=1):
        move = chess.Move.from_uci(uci)
        if move not in b.legal_moves:
            break
        b.push(move)
        if b.is_game_over():
            break
        if i % 2 == 0:
            out.append(b.copy(stack=False))
    return out


def commitment_from_sharpness(sharpness: Sequence[float]) -> float | None:
    return None if len(sharpness) < 2 else max(sharpness) - min(sharpness)


# --------------------------------------------------------------------------- engine-driven
def sharpness(board: chess.Board, cand: dict, depth: int, engine: Engine, cache: AnalysisCache
              ) -> float | None:
    points = user_decision_points(board, cand.get("pv") or [cand["move"]])
    if not points:
        return None
    cs = [analyze_position(p, depth, engine, cache).criticality for p in points]
    return sum(cs) / len(cs)


def commitment_for(board: chess.Board, cands: Sequence[dict], depth: int, engine: Engine,
                   cache: AnalysisCache) -> tuple[float | None, dict]:
    sharp = {}
    for c in cands:
        s = sharpness(board, c, depth, engine, cache)
        if s is not None:
            sharp[c["move"]] = round(s, 4)
    return commitment_from_sharpness(list(sharp.values())), sharp


def candidates_for_forks(con: sqlite3.Connection, recompute: bool, limit: int | None) -> list[sqlite3.Row]:
    sql = """SELECT p.id, p.fen, p.criticality, p.obvious, a.candidates_json
               FROM positions p JOIN analysis a ON a.id = p.analysis_id
              WHERE p.user_to_move = 1 AND p.criticality IS NOT NULL AND p.criticality <= ?"""
    if not recompute:
        sql += " AND p.commitment IS NULL"
    sql += " ORDER BY p.game_id DESC, p.ply"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return con.execute(sql, (config.CALM,)).fetchall()


def run_forks(con: sqlite3.Connection, *, depth: int = config.FORK_DEPTH, recompute: bool = False,
              limit: int | None = None, engine: Engine | None = None, log: Log = print) -> dict:
    """Compute commitment for every gated position; upgrade labels where fork. Commits per 25."""
    import json

    rows = candidates_for_forks(con, recompute, limit)
    stats = {"visited": len(rows), "gated": 0, "computed": 0, "forks": 0, "upgraded": 0}
    if not rows:
        log("[forks] nothing to do")
        return stats
    own_engine = engine is None
    engine = engine or Engine()
    cache = AnalysisCache(con)
    t0 = time.perf_counter()
    try:
        for i, r in enumerate(rows, 1):
            board = chess.Board(r["fen"])
            cands = passes_gate(board, r["criticality"], json.loads(r["candidates_json"]))
            if not cands:
                # Visited but not gated: 0.0 marks "checked, no fork" so re-runs skip it.
                con.execute("UPDATE positions SET commitment = 0.0 WHERE id = ?", (r["id"],))
                continue
            stats["gated"] += 1
            commitment, sharp = commitment_for(board, cands, depth, engine, cache)
            if commitment is None:
                con.execute("UPDATE positions SET commitment = 0.0 WHERE id = ?", (r["id"],))
                continue
            stats["computed"] += 1
            fork = commitment >= config.FORK
            new_label = label(r["criticality"], bool(r["obvious"]), fork=fork)
            if fork:
                stats["forks"] += 1
                if new_label != con.execute("SELECT label FROM positions WHERE id = ?", (r["id"],)).fetchone()[0]:
                    stats["upgraded"] += 1
            con.execute("UPDATE positions SET commitment = ?, label = ? WHERE id = ?",
                        (round(commitment, 4), new_label, r["id"]))
            if i % 25 == 0:
                con.commit()
                log(f"[forks] {i}/{len(rows)} gated={stats['gated']} forks={stats['forks']} "
                    f"searches={engine.searches} {time.perf_counter() - t0:.0f}s")
        con.commit()
    finally:
        if own_engine:
            engine.close()
    log(f"[forks] visited={stats['visited']} gated={stats['gated']} computed={stats['computed']} "
        f"forks={stats['forks']} label_upgrades={stats['upgraded']} searches={engine.searches} "
        f"{time.perf_counter() - t0:.0f}s")
    return stats
