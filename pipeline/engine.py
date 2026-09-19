"""A2: Stockfish wrapper with a position cache in the `analysis` table.

Two engine processes:
  * `deep`    - Threads = cores-1, Hash = HASH_MB, MultiPV = 4 at ANALYSIS_DEPTH.
  * `shallow` - Threads = 1, tiny hash, a fresh game (empty transposition table) for
                every probe. If the shallow probe shared the deep engine's hash table it
                would read the deep result straight back and `obvious` would be inflated.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import chess
import chess.engine

from pipeline import config
from pipeline.metrics import criticality, expected_points, fen_key


@dataclass
class AnalysisResult:
    fen_key: str
    depth: int
    best_move: str                       # UCI
    shallow_best_move: str               # UCI, at SHALLOW_DEPTH
    candidates: list[dict] = field(default_factory=list)   # best first; see schema comment
    id: int | None = None

    @property
    def e_best(self) -> float:
        return float(self.candidates[0]["e"])

    @property
    def criticality(self) -> float:
        return criticality(self.candidates)

    @property
    def obvious(self) -> bool:
        return self.best_move == self.shallow_best_move

    @property
    def candidates_json(self) -> str:
        return json.dumps(self.candidates, separators=(",", ":"))

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AnalysisResult":
        return cls(fen_key=row["fen_key"], depth=row["depth"], best_move=row["best_move"],
                   shallow_best_move=row["shallow_best_move"],
                   candidates=json.loads(row["candidates_json"]), id=row["id"])


def score_fields(score: chess.engine.Score) -> tuple[int | None, int | None, float]:
    """(cp, mate, E) for a score already in the mover's point of view."""
    mate = score.mate()
    cp = None if mate is not None else score.score()
    return cp, mate, expected_points(cp, mate)


class Engine:
    def __init__(self, path: str | None = None, *, threads: int = config.THREADS,
                 hash_mb: int = config.HASH_MB, shallow_depth: int = config.SHALLOW_DEPTH):
        path = path or config.stockfish_path()
        self.path = path
        self.shallow_depth = shallow_depth
        self.deep = chess.engine.SimpleEngine.popen_uci(path)
        self.deep.configure({"Threads": threads, "Hash": hash_mb})
        self.shallow = chess.engine.SimpleEngine.popen_uci(path)
        self.shallow.configure({"Threads": config.SHALLOW_THREADS, "Hash": config.SHALLOW_HASH_MB})
        self.searches = 0
        self.capped = 0                  # deep searches stopped by the time cap before reaching depth

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        for eng in (self.deep, self.shallow):
            try:
                eng.quit()
            except chess.engine.EngineError:
                pass

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ searches
    def shallow_best(self, board: chess.Board) -> str:
        """Best move at SHALLOW_DEPTH from an empty hash table."""
        info = self.shallow.analyse(board, chess.engine.Limit(depth=self.shallow_depth),
                                    game=object())
        pv = info.get("pv")
        if pv:
            return pv[0].uci()
        return self.shallow.play(board, chess.engine.Limit(depth=self.shallow_depth),
                                 game=object()).move.uci()

    def search(self, board: chess.Board, depth: int, multipv: int = config.MULTIPV) -> AnalysisResult:
        """Uncached MultiPV search at `depth` (capped at MAX_SEARCH_SECONDS) plus the shallow probe."""
        if board.is_game_over():
            raise ValueError("terminal position has no candidates")
        limit = chess.engine.Limit(depth=depth, time=config.MAX_SEARCH_SECONDS)
        infos = self.deep.analyse(board, limit, multipv=multipv)
        if infos and infos[0].get("depth", depth) < depth:
            self.capped += 1
        infos = sorted(infos, key=lambda i: i.get("multipv", 1))
        candidates: list[dict] = []
        for info in infos:
            pv = info.get("pv")
            if not pv or "score" not in info:
                continue
            cp, mate, e = score_fields(info["score"].relative)   # relative = mover's POV
            candidates.append({
                "move": pv[0].uci(), "cp": cp, "mate": mate, "e": round(e, 4),
                "pv": [m.uci() for m in pv[:config.PV_MAX_PLIES]],
            })
        if not candidates:
            raise RuntimeError(f"engine returned no lines for {board.fen()}")
        self.searches += 1
        return AnalysisResult(fen_key=fen_key(board), depth=depth,
                              best_move=candidates[0]["move"],
                              shallow_best_move=self.shallow_best(board),
                              candidates=candidates)


class AnalysisCache:
    """Read-through cache over the `analysis` table keyed by (fen_key, depth).

    Writes are not committed here: the caller commits once per game.
    """

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.mem: dict[tuple[str, int], AnalysisResult] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str, depth: int) -> AnalysisResult | None:
        k = (key, depth)
        if k in self.mem:
            self.hits += 1
            return self.mem[k]
        row = self.con.execute("SELECT * FROM analysis WHERE fen_key = ? AND depth = ?", k).fetchone()
        if row is None:
            self.misses += 1
            return None
        res = AnalysisResult.from_row(row)
        self.mem[k] = res
        self.hits += 1
        return res

    def put(self, res: AnalysisResult) -> AnalysisResult:
        """Insert, or upgrade an existing row that has fewer candidate lines than `res`."""
        self.con.execute(
            """INSERT OR IGNORE INTO analysis (fen_key, depth, best_move, shallow_best_move, candidates_json)
               VALUES (?,?,?,?,?)""",
            (res.fen_key, res.depth, res.best_move, res.shallow_best_move, res.candidates_json))
        row = self.con.execute("SELECT id, candidates_json FROM analysis WHERE fen_key = ? AND depth = ?",
                               (res.fen_key, res.depth)).fetchone()
        res.id = row["id"]
        if len(json.loads(row["candidates_json"])) < len(res.candidates):
            self.con.execute("UPDATE analysis SET best_move = ?, shallow_best_move = ?, candidates_json = ? "
                             "WHERE id = ?", (res.best_move, res.shallow_best_move, res.candidates_json, res.id))
        self.mem[(res.fen_key, res.depth)] = res
        return res

    def update_shallow(self, res: AnalysisResult, shallow_best_move: str) -> None:
        res.shallow_best_move = shallow_best_move
        self.con.execute("UPDATE analysis SET shallow_best_move = ? WHERE id = ?",
                         (shallow_best_move, res.id))


def analyze_position(board: chess.Board, depth: int, engine: Engine, cache: AnalysisCache,
                     multipv: int = config.MULTIPV) -> AnalysisResult:
    """Cached analysis of one (non-terminal) position with at least `multipv` lines.

    A cached row with fewer lines than requested (an opponent-ply search that is now
    needed as a user decision point) is re-searched and upgraded in place.
    """
    key = fen_key(board)
    need = min(multipv, board.legal_moves.count())
    res = cache.get(key, depth)
    if res is None or len(res.candidates) < need:
        res = cache.put(engine.search(board, depth, multipv=multipv))
    return res
