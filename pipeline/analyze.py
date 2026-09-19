"""A2 (second half): fill `analysis` for every ply of a game, then score each position.

Per position (all from the mover's point of view, SPEC.md §2):
    e_best      = E of the engine's best move
    e_played    = 1 - e_best(next ply)           # the played move is the next ply, already analyzed
    e_loss      = e_best - e_played              # clamped at 0
    criticality = E(best) - mean(E(2nd..4th))
    obvious     = shallow best == deep best
    label       = LONG / SHORT / GRAY            # fork kept if the commitment pass already ran

User-to-move plies get the full MultiPV search. Opponent plies only exist to provide the
next-ply E, so they get OPPONENT_MULTIPV lines and no criticality/label (NULL).

Commit once per game so a crash loses at most one game. `--workers N` runs N engine
processes on disjoint games; SQLite serialises the writes.
"""
from __future__ import annotations

import multiprocessing as mp
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable

import chess

from pipeline import config, db
from pipeline.engine import AnalysisCache, AnalysisResult, Engine, analyze_position
from pipeline.metrics import is_fork, label

Log = Callable[[str], None]


def terminal_e_best(board: chess.Board) -> float:
    """E for the side to move in a finished position: mated -> 0, any draw -> 0.5."""
    return 0.0 if board.is_checkmate() else 0.5


def score_positions(positions: list, results: list[AnalysisResult],
                    e_best_after_last: float) -> list[dict]:
    """Pure scoring step. `positions` and `results` are aligned and ordered by ply.

    Rows whose analysis has fewer lines than the position needs (opponent plies searched
    with OPPONENT_MULTIPV) get NULL criticality/label: they are never drilled.
    """
    out: list[dict] = []
    for i, (p, res) in enumerate(zip(positions, results)):
        e_best = res.e_best
        next_e_best = results[i + 1].e_best if i + 1 < len(results) else e_best_after_last
        e_played = 1.0 - next_e_best
        e_loss = max(0.0, e_best - e_played)
        need = min(config.MULTIPV, chess.Board(p["fen"]).legal_moves.count()) if "fen" in p.keys() else 1
        full = len(res.candidates) >= need
        crit = res.criticality if full else None
        obvious = res.obvious
        out.append({
            "id": p["id"],
            "analysis_id": res.id,
            "e_best": round(e_best, 4),
            "e_played": round(e_played, 4),
            "e_loss": round(e_loss, 4),
            "criticality": None if crit is None else round(crit, 4),
            "obvious": int(obvious),
            "label": label(crit, obvious, fork=is_fork(p["commitment"])) if full else None,
        })
    return out


def analyze_game(con: sqlite3.Connection, game_id: int, depth: int, engine: Engine,
                 cache: AnalysisCache) -> int:
    """Analyze and score every ply of one game; returns the number of plies scored."""
    positions = con.execute(
        "SELECT id, ply, fen, move_played, user_to_move, commitment FROM positions "
        "WHERE game_id = ? ORDER BY ply", (game_id,)).fetchall()
    if not positions:
        return 0
    results = [
        analyze_position(chess.Board(p["fen"]), depth, engine, cache,
                         multipv=config.MULTIPV if p["user_to_move"] else config.OPPONENT_MULTIPV)
        for p in positions
    ]

    # The position after the final move has no `positions` row but is needed for the
    # last ply's e_played. Cache it in `analysis` unless the game is over there.
    last = chess.Board(positions[-1]["fen"])
    last.push_uci(positions[-1]["move_played"])
    if last.is_game_over():
        e_after = terminal_e_best(last)
    else:
        e_after = analyze_position(last, depth, engine, cache, multipv=config.OPPONENT_MULTIPV).e_best

    con.executemany(
        """UPDATE positions SET analysis_id = :analysis_id, e_best = :e_best, e_played = :e_played,
               e_loss = :e_loss, criticality = :criticality, obvious = :obvious, label = :label
           WHERE id = :id""",
        score_positions(positions, results, e_after))
    con.execute("UPDATE games SET analyzed_at = ?, analysis_depth = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), depth, game_id))
    con.commit()
    return len(positions)


def pending_games(con: sqlite3.Connection, depth: int, limit: int | None) -> list[sqlite3.Row]:
    sql = """SELECT id, url, played_at FROM games
              WHERE analyzed_at IS NULL OR analysis_depth IS NOT ? ORDER BY played_at DESC"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    return con.execute(sql, (depth,)).fetchall()


def _run_games(con: sqlite3.Connection, games: list, depth: int, engine: Engine, log: Log,
               tag: str = "") -> int:
    cache = AnalysisCache(con)
    done = 0
    for g in games:
        t0 = time.perf_counter()
        before = engine.searches
        plies = analyze_game(con, g["id"], depth, engine, cache)
        done += 1
        log(f"[analyze{tag}] {done}/{len(games)} {g['url']} plies={plies} "
            f"searches={engine.searches - before} capped={engine.capped} "
            f"{time.perf_counter() - t0:.1f}s")
    log(f"[analyze{tag}] done: games={done} searches={engine.searches} capped={engine.capped} "
        f"cache_hits={cache.hits}")
    return done


def _worker(args: tuple) -> int:
    """Entry point for one engine process: own connection, own engine, its share of games."""
    db_path, game_ids, depth, threads, idx = args
    con = db.connect(db_path)
    con.execute("PRAGMA busy_timeout = 60000")
    rows = [con.execute("SELECT id, url, played_at FROM games WHERE id = ?", (gid,)).fetchone()
            for gid in game_ids]
    try:
        with Engine(threads=threads, hash_mb=max(64, config.HASH_MB // max(1, config.WORKERS))) as engine:
            return _run_games(con, rows, depth, engine, print, tag=f" w{idx}")
    finally:
        con.close()


def analyze(con: sqlite3.Connection, *, depth: int = config.ANALYSIS_DEPTH, limit: int | None = None,
            engine: Engine | None = None, workers: int = 1, log: Log = print) -> int:
    """Analyze every game not yet scored at `depth`. Returns the number of games processed."""
    games = pending_games(con, depth, limit)
    if not games:
        log(f"[analyze] nothing to do at depth {depth}")
        return 0
    log(f"[analyze] {len(games)} game(s) at depth {depth}, shallow {config.SHALLOW_DEPTH}, "
        f"cap {config.MAX_SEARCH_SECONDS}s, opponent multipv {config.OPPONENT_MULTIPV}, "
        f"workers {workers}, threads {config.THREADS}")

    if workers > 1 and engine is None:
        db_path = con.execute("PRAGMA database_list").fetchone()[2]
        config.WORKERS = workers
        threads = max(1, config.THREADS // workers)
        shares = [[g["id"] for g in games[i::workers]] for i in range(workers)]
        con.execute("PRAGMA busy_timeout = 60000")
        with mp.get_context("spawn").Pool(workers) as pool:
            counts = pool.map(_worker, [(db_path, share, depth, threads, i) for i, share in enumerate(shares)])
        log(f"[analyze] all workers done: games={sum(counts)}")
        return sum(counts)

    own_engine = engine is None
    engine = engine or Engine()
    try:
        return _run_games(con, games, depth, engine, log)
    finally:
        if own_engine:
            engine.close()


def reshallow(con: sqlite3.Connection, *, engine: Engine | None = None, log: Log = print) -> int:
    """Recompute shallow_best_move for every cached analysis row at the current SHALLOW_DEPTH,
    then re-derive obvious/label. Cheap; used after retuning SHALLOW_DEPTH (the cache key does
    not include the shallow depth)."""
    own_engine = engine is None
    engine = engine or Engine()
    rows = con.execute("SELECT id, fen_key, best_move, shallow_best_move FROM analysis").fetchall()
    changed = 0
    try:
        for i, row in enumerate(rows, 1):
            board = chess.Board(row["fen_key"] + " 0 1")
            new = engine.shallow_best(board)
            if new != row["shallow_best_move"]:
                changed += 1
                con.execute("UPDATE analysis SET shallow_best_move = ? WHERE id = ?", (new, row["id"]))
            if i % 500 == 0:
                con.commit()
                log(f"[reshallow] {i}/{len(rows)} changed={changed}")
        con.commit()
    finally:
        if own_engine:
            engine.close()
    con.execute("""UPDATE positions SET obvious = (SELECT a.best_move = a.shallow_best_move
                                                     FROM analysis a WHERE a.id = positions.analysis_id)
                    WHERE analysis_id IS NOT NULL""")
    for p in con.execute("SELECT id, criticality, obvious, commitment FROM positions "
                         "WHERE analysis_id IS NOT NULL AND criticality IS NOT NULL").fetchall():
        con.execute("UPDATE positions SET label = ? WHERE id = ?",
                    (label(p["criticality"], bool(p["obvious"]), is_fork(p["commitment"])), p["id"]))
    con.commit()
    log(f"[reshallow] rows={len(rows)} changed={changed} at shallow depth {engine.shallow_depth}")
    return changed
