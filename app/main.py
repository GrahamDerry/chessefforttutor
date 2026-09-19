"""Chess Effort Tutor — drill API and static app (SPEC.md §5).

Run:  TUTOR_DB=data/fake.db uvicorn app.main:app --reload
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import chess
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db as dbmod
from app.explain import explain, why_label

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Chess Effort Tutor")

_con: sqlite3.Connection | None = None


def con() -> sqlite3.Connection:
    global _con
    if _con is None:
        _con = dbmod.connect()
    return _con


def set_connection(c: sqlite3.Connection | None) -> None:
    """Test hook — point the app at a temp database."""
    global _con
    _con = c


# --------------------------------------------------------------------------- models


class Answer(BaseModel):
    scenario_id: int
    answer: Literal["LONG", "SHORT"]
    response_ms: int | None = Field(default=None, ge=0)


# --------------------------------------------------------------------------- helpers


def _san_line(fen: str, ucis: list[str], limit: int = 8) -> list[str]:
    """Convert a UCI principal variation to SAN for display."""
    board = chess.Board(fen)
    out: list[str] = []
    for uci in ucis[:limit]:
        try:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                break
            out.append(board.san(move))
            board.push(move)
        except ValueError:
            break
    return out


def _best_line(row: sqlite3.Row) -> tuple[str | None, list[str]]:
    if not row["candidates_json"]:
        return None, []
    try:
        cands = json.loads(row["candidates_json"])
    except json.JSONDecodeError:
        return None, []
    if not cands:
        return None, []
    pv = cands[0].get("pv") or [cands[0]["move"]]
    line = _san_line(row["fen"], pv)
    return (line[0] if line else None), line


def _pick(exclude: set[int]) -> sqlite3.Row | None:
    """A scenario the user has not just seen, balanced LONG/SHORT.

    Coin-flip the target label first so the drill stays ~50:50 regardless of how
    the underlying scenario mix is skewed; within a label, prefer positions the
    user has never attempted, then least-attempted.
    """
    order = ["LONG", "SHORT"]
    random.shuffle(order)
    for target in order:
        rows = con().execute(
            """SELECT s.id,
                      (SELECT COUNT(*) FROM drill_attempts d WHERE d.scenario_id = s.id) AS seen
                 FROM scenarios s
                WHERE s.ground_truth = ?""",
            (target,),
        ).fetchall()
        pool = [r for r in rows if r["id"] not in exclude]
        if not pool:
            continue
        fewest = min(r["seen"] for r in pool)
        candidates = [r["id"] for r in pool if r["seen"] == fewest]
        return dbmod.get_scenario(con(), random.choice(candidates))
    return None


# --------------------------------------------------------------------------- endpoints


@app.get("/api/drill/next")
def drill_next(exclude: str = Query("", description="comma-separated scenario ids")):
    """A position to triage.

    Deliberately minimal: board, whose move, and the clock as it stood. Anything
    that hints at the answer — kind, ground_truth, evals, the result of the game —
    is withheld until the user has committed.
    """
    skip = {int(x) for x in exclude.split(",") if x.strip().isdigit()}
    row = _pick(skip)
    if row is None:
        raise HTTPException(404, "No scenarios available. Has the pipeline run?")
    board = chess.Board(row["fen"])
    return {
        "scenario_id": row["scenario_id"],
        "fen": row["fen"],
        "user_color": row["user_color"],
        "side_to_move": "white" if board.turn == chess.WHITE else "black",
        "ply": row["ply"],
        "move_number": board.fullmove_number,
        "clock_before": row["clock_before"],
        "base_seconds": row["base_seconds"],
        "increment": row["increment"],
    }


@app.post("/api/drill/answer")
def drill_answer(payload: Answer):
    row = dbmod.get_scenario(con(), payload.scenario_id)
    if row is None:
        raise HTTPException(404, f"No scenario {payload.scenario_id}")
    correct = payload.answer == row["ground_truth"]
    con().execute(
        """INSERT INTO drill_attempts (scenario_id, answer, correct, response_ms, answered_at)
           VALUES (?,?,?,?,?)""",
        (payload.scenario_id, payload.answer, int(correct), payload.response_ms,
         datetime.now(timezone.utc).isoformat()),
    )
    con().commit()

    best_san, pv_san = _best_line(row)
    return {
        "correct": correct,
        "ground_truth": row["ground_truth"],
        "kind": row["kind"],
        "criticality": row["criticality"],
        "obvious": bool(row["obvious"]) if row["obvious"] is not None else None,
        "commitment": row["commitment"],
        "move_played_san": row["move_san"],
        "seconds_spent": row["seconds_spent"],
        "time_fraction": row["time_fraction"],
        "e_loss": row["e_loss"],
        "e_best": row["e_best"],
        "best_move_san": best_san,
        "pv_san": pv_san,
        "game_url": row["game_url"],
        "result_user": row["result_user"],
        "phase": row["phase"],
        "why": why_label(row),
        "explanation": explain(row),
    }


@app.get("/api/stats")
def stats():
    c = con()
    total, correct = c.execute(
        "SELECT COUNT(*), COALESCE(SUM(correct), 0) FROM drill_attempts"
    ).fetchone()

    def breakdown(column: str) -> dict:
        rows = c.execute(
            f"""SELECT {column} AS k, COUNT(*) n, SUM(d.correct) ok
                  FROM drill_attempts d
                  JOIN scenarios s ON s.id = d.scenario_id
                  JOIN positions p ON p.id = s.position_id
                 GROUP BY 1 ORDER BY n DESC"""
        ).fetchall()
        return {r["k"]: {"n": r["n"], "accuracy": (r["ok"] or 0) / r["n"]}
                for r in rows if r["k"] is not None}

    recent = [dict(r) for r in c.execute(
        """SELECT d.answered_at, d.answer, d.correct, d.response_ms,
                  s.kind, s.ground_truth
             FROM drill_attempts d JOIN scenarios s ON s.id = d.scenario_id
            ORDER BY d.id DESC LIMIT 50"""
    ).fetchall()]

    pool = dict(c.execute(
        "SELECT ground_truth, COUNT(*) FROM scenarios GROUP BY 1").fetchall())

    return {
        "attempts": total,
        "accuracy": (correct / total) if total else None,
        "accuracy_by_kind": breakdown("s.kind"),
        "accuracy_by_phase": breakdown("p.phase"),
        "accuracy_by_truth": breakdown("s.ground_truth"),
        "pool": pool,
        "recent": recent,
    }


@app.get("/api/scenarios/{scenario_id}")
def scenario_detail(scenario_id: int):
    row = dbmod.get_scenario(con(), scenario_id)
    if row is None:
        raise HTTPException(404, f"No scenario {scenario_id}")
    best_san, pv_san = _best_line(row)
    out = {k: row[k] for k in row.keys() if k != "candidates_json"}
    out["best_move_san"] = best_san
    out["pv_san"] = pv_san
    out["explanation"] = explain(row)
    out["attempts"] = [dict(r) for r in con().execute(
        "SELECT answer, correct, response_ms, answered_at FROM drill_attempts "
        "WHERE scenario_id = ? ORDER BY id DESC", (scenario_id,)).fetchall()]
    return out


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/stats")
def stats_page():
    return FileResponse(STATIC / "stats.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
