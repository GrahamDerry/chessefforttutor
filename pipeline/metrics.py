"""Pure functions implementing the shared vocabulary of SPEC.md §2.

No I/O, no engine, no database: everything here is unit-testable from a dict.
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence

import chess

from pipeline import config


# --------------------------------------------------------------------------- expected points
def expected_points(cp: float | None, mate: int | None = None) -> float:
    """E from the mover's point of view. Mate scores collapse to 1.0 / 0.0.

    `mate` follows python-chess: positive = mover mates, negative = mover is mated,
    0 = mover is already checkmated.
    """
    if mate is not None:
        return 1.0 if mate > 0 else 0.0
    if cp is None:
        raise ValueError("need cp or mate")
    return 1.0 / (1.0 + math.exp(-config.E_SCALE * cp))


E = expected_points


# --------------------------------------------------------------------------- position keys
def fen_key(fen_or_board: str | chess.Board) -> str:
    """FEN with the halfmove and fullmove counters stripped (first four fields).

    python-chess only writes an en-passant square when a capture is actually legal,
    so unusable ep squares are already normalised away.
    """
    fen = fen_or_board.fen() if isinstance(fen_or_board, chess.Board) else fen_or_board
    return " ".join(fen.split(" ")[:4])


# --------------------------------------------------------------------------- criticality / label
def criticality(candidates: Sequence[Mapping]) -> float:
    """C = E(best) - mean(E(2nd), E(3rd), E(4th)) over whatever candidates exist.

    `candidates` is the decoded candidates_json: best first, each with an "e" key.
    One legal move -> 0.0.
    """
    if len(candidates) < 2:
        return 0.0
    rest = [float(c["e"]) for c in candidates[1:config.MULTIPV]]
    return float(candidates[0]["e"]) - sum(rest) / len(rest)


def label(crit: float, obvious: bool, fork: bool = False) -> str:
    """Ground-truth label from the position alone (SPEC.md §2).

    `obvious` means a shallow probe already found the deep search's move, so fast
    intuition would have done. It forces SHORT, but only below
    OBVIOUS_VETO_MAX_CRIT: a position sharp enough to clear CRIT still deserves
    thought even when the probe got lucky. Set OBVIOUS_VETO_MAX_CRIT = 1.0 for the
    old unconditional veto.
    """
    if fork:
        return "LONG"
    if obvious and crit < config.OBVIOUS_VETO_MAX_CRIT:
        return "SHORT"
    if crit <= config.CALM:
        return "SHORT"
    if crit >= config.CRIT:
        return "LONG"
    return "GRAY"


def is_fork(commitment: float | None) -> bool:
    return commitment is not None and commitment >= config.FORK


# --------------------------------------------------------------------------- clocks
def parse_clock(text: str) -> float:
    """'0:04:57.6' -> 297.6 seconds. Accepts H:MM:SS(.t), MM:SS or SS."""
    total = 0.0
    for part in text.strip().split(":"):
        total = total * 60 + float(part)
    return total


def parse_time_control(tc: str) -> tuple[int, int]:
    """Chess.com time_control '300' -> (300, 0); '300+5' -> (300, 5). Daily ('1/86400') raises."""
    if "/" in tc:
        raise ValueError(f"not a live time control: {tc!r}")
    base, _, inc = tc.partition("+")
    return int(base), int(inc or 0)


def seconds_spent(clock_before: float, clock_after: float, increment: float) -> float:
    """%clk is time REMAINING after the move, so the increment must be added back.

    Lag compensation can make a premove come out a tenth negative; clamp to 0.
    """
    return max(0.0, clock_before - clock_after + increment)


def time_fraction(spent: float, clock_before: float | None) -> float | None:
    if clock_before is None or clock_before <= 0:
        return None
    return spent / clock_before


# --------------------------------------------------------------------------- phase
def non_pawn_material(board: chess.Board) -> int:
    return sum(config.PIECE_VALUES[pt] * len(board.pieces(pt, color))
               for pt in config.PIECE_VALUES for color in chess.COLORS)


def phase(board: chess.Board, ply: int) -> str:
    """opening: ply <= 24 (v1). endgame: no queens, or little non-pawn material. else middlegame."""
    if ply <= config.OPENING_MAX_PLY:
        return "opening"
    no_queens = not (board.pieces(chess.QUEEN, chess.WHITE) or board.pieces(chess.QUEEN, chess.BLACK))
    if no_queens or non_pawn_material(board) <= config.ENDGAME_MATERIAL:
        return "endgame"
    return "middlegame"


# --------------------------------------------------------------------------- results
def result_for(result_tag: str, user_color: str) -> float:
    """PGN Result tag + the user's colour -> 1.0 / 0.5 / 0.0."""
    if result_tag == "1/2-1/2":
        return 0.5
    if result_tag not in ("1-0", "0-1"):
        raise ValueError(f"unfinished or unknown result {result_tag!r}")
    white_won = result_tag == "1-0"
    return 1.0 if white_won == (user_color == "white") else 0.0


def percentile(values: Iterable[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for an empty input."""
    xs = sorted(values)
    if not xs:
        return None
    k = max(0, min(len(xs) - 1, math.ceil(pct / 100.0 * len(xs)) - 1))
    return xs[k]
