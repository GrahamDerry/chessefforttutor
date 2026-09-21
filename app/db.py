"""SQLite access for the drill app.

The app reads everything and writes only drill_attempts (SPEC.md §5).
"""
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "pipeline" / "schema.sql"


def db_path() -> Path:
    return Path(os.environ.get("TUTOR_DB", ROOT / "data" / "tutor.db"))


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    if not p.exists():
        raise FileNotFoundError(
            f"No database at {p}. Try the sample data with TUTOR_DB=data/fixture.db, "
            f"or build your own with `python -m pipeline ingest` then `python -m pipeline analyze` "
            f"(see README.md)."
        )
    con = sqlite3.connect(p, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_empty(path: Path) -> sqlite3.Connection:
    """Create an empty DB from the canonical schema (tests use this)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    return con


# The join every endpoint needs: scenario + its position + that position's game.
SCENARIO_JOIN = """
  SELECT s.id           AS scenario_id,
         s.kind, s.ground_truth, s.severity, s.notes_json,
         p.id           AS position_id,
         p.game_id, p.fen, p.ply, p.phase, p.move_san, p.move_played,
         p.clock_before, p.clock_after, p.seconds_spent, p.time_fraction,
         p.e_best, p.e_played, p.e_loss, p.criticality, p.obvious, p.commitment, p.label,
         g.url AS game_url, g.user_color, g.base_seconds, g.increment,
         g.result_user, g.time_class, g.opp_rating, g.user_rating,
         a.best_move, a.shallow_best_move, a.candidates_json
    FROM scenarios s
    JOIN positions p ON p.id = s.position_id
    JOIN games     g ON g.id = p.game_id
    LEFT JOIN analysis a ON a.id = p.analysis_id
"""


def get_scenario(con: sqlite3.Connection, scenario_id: int) -> sqlite3.Row | None:
    return con.execute(SCENARIO_JOIN + " WHERE s.id = ?", (scenario_id,)).fetchone()


def get_history(con: sqlite3.Connection, game_id: int, ply: int,
                limit: int = 5) -> list[sqlite3.Row]:
    """The last `limit` plies of a game strictly before `ply`, in ascending order.

    Each row's `fen` is the position *before* `move_played`, so replaying the rows in
    order walks the board up to the position at `ply`.
    """
    rows = con.execute(
        """SELECT ply, fen, move_played, move_san
             FROM positions
            WHERE game_id = ? AND ply < ?
            ORDER BY ply DESC LIMIT ?""",
        (game_id, ply, limit),
    ).fetchall()
    return rows[::-1]
