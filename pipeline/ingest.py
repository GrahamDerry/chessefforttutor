"""A1: Chess.com archives -> `games` + `positions` (every column except the analysis fields).

Idempotent: a game whose url is already in `games` is skipped. One transaction per game.
"""
from __future__ import annotations

import io
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import chess
import chess.pgn

from pipeline import config
from pipeline.chesscom import ChessComClient, select_archives, year_month
from pipeline.metrics import (parse_time_control, phase, result_for, seconds_spent,
                              time_fraction)

Log = Callable[[str], None]


@dataclass
class IngestStats:
    months: int = 0
    fetched: int = 0
    inserted: int = 0
    skipped_time_class: int = 0
    skipped_rules: int = 0
    skipped_no_clock: int = 0
    skipped_existing: int = 0
    skipped_unparseable: int = 0
    negative_clock_clamped: int = 0
    plies_without_clock: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"months={self.months} fetched={self.fetched} inserted={self.inserted} | skipped: "
                f"time_class={self.skipped_time_class} rules={self.skipped_rules} "
                f"no_clock={self.skipped_no_clock} existing={self.skipped_existing} "
                f"unparseable={self.skipped_unparseable} | negative_clock_clamped="
                f"{self.negative_clock_clamped} plies_without_clock={self.plies_without_clock}")


# --------------------------------------------------------------------------- parsing
def qualifies(g: dict, stats: IngestStats) -> bool:
    if g.get("time_class") != config.TIME_CLASS:
        stats.skipped_time_class += 1
        return False
    if g.get("rules", config.RULES) != config.RULES:
        stats.skipped_rules += 1
        return False
    if "%clk" not in g.get("pgn", ""):
        stats.skipped_no_clock += 1
        return False
    return True


def _played_at(headers: chess.pgn.Headers, g: dict) -> str:
    d, t = headers.get("UTCDate"), headers.get("UTCTime")
    if d and t and "?" not in d:
        return f"{d.replace('.', '-')}T{t}Z"
    return datetime.fromtimestamp(int(g["end_time"]), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_game(g: dict, username: str = config.USERNAME,
               stats: IngestStats | None = None) -> tuple[dict, list[dict]]:
    """One Chess.com game JSON -> (games row, positions rows). Raises ValueError if unusable."""
    stats = stats or IngestStats()
    game = chess.pgn.read_game(io.StringIO(g["pgn"]))
    if game is None:
        raise ValueError("PGN did not parse")
    headers = game.headers

    uname = username.lower()
    if g["white"]["username"].lower() == uname:
        user_color, user_is_white = "white", True
    elif g["black"]["username"].lower() == uname:
        user_color, user_is_white = "black", False
    else:
        raise ValueError(f"{username} is not a player in {g.get('url')}")

    base, inc = parse_time_control(g["time_control"])
    result = result_for(headers.get("Result", "*"), user_color)
    me, opp = (g["white"], g["black"]) if user_is_white else (g["black"], g["white"])

    game_row = {
        "url": headers.get("Link") or g["url"],
        "played_at": _played_at(headers, g),
        "time_class": g["time_class"],
        "base_seconds": base,
        "increment": inc,
        "user_color": user_color,
        "user_rating": me.get("rating"),
        "opp_name": opp.get("username"),
        "opp_rating": opp.get("rating"),
        "result_user": result,
        "eco": headers.get("ECO"),
        "eco_url": headers.get("ECOUrl"),
        "termination": headers.get("Termination"),
        "pgn": g["pgn"],
    }

    positions: list[dict] = []
    board = game.board()
    user_color_bool = chess.WHITE if user_is_white else chess.BLACK
    # Each side's first move compares against base_seconds.
    remaining = {chess.WHITE: float(base), chess.BLACK: float(base)}
    for node in game.mainline():
        move = node.move
        ply = board.ply() + 1                       # 1 = White's first move
        mover = board.turn
        clock_before = remaining[mover]
        clock_after = node.clock()                  # time REMAINING after the move, tenths kept
        if clock_after is None:
            stats.plies_without_clock += 1
            spent = frac = None
        else:
            raw = clock_before - clock_after + inc
            if raw < 0:
                stats.negative_clock_clamped += 1
            spent = seconds_spent(clock_before, clock_after, inc)
            frac = time_fraction(spent, clock_before)
            remaining[mover] = clock_after
        positions.append({
            "ply": ply,
            "fen": board.fen(),
            "user_to_move": int(mover == user_color_bool),
            "phase": phase(board, ply),
            "move_played": move.uci(),
            "move_san": board.san(move),
            "clock_before": clock_before,
            "clock_after": clock_after,
            "seconds_spent": None if spent is None else round(spent, 1),
            "time_fraction": None if frac is None else round(frac, 6),
        })
        board.push(move)

    if not positions:
        raise ValueError("game has no moves")
    return game_row, positions


# --------------------------------------------------------------------------- writing
def insert_game(con: sqlite3.Connection, game_row: dict, positions: list[dict]) -> int:
    """Insert a game and all its plies in one transaction."""
    cur = con.execute(
        """INSERT INTO games (url, played_at, time_class, base_seconds, increment, user_color,
               user_rating, opp_name, opp_rating, result_user, eco, eco_url, termination, pgn)
           VALUES (:url, :played_at, :time_class, :base_seconds, :increment, :user_color,
               :user_rating, :opp_name, :opp_rating, :result_user, :eco, :eco_url, :termination, :pgn)""",
        game_row)
    game_id = cur.lastrowid
    con.executemany(
        """INSERT INTO positions (game_id, ply, fen, user_to_move, phase, move_played, move_san,
               clock_before, clock_after, seconds_spent, time_fraction)
           VALUES (:game_id, :ply, :fen, :user_to_move, :phase, :move_played, :move_san,
               :clock_before, :clock_after, :seconds_spent, :time_fraction)""",
        [{**p, "game_id": game_id} for p in positions])
    con.commit()
    return game_id


def game_exists(con: sqlite3.Connection, url: str) -> bool:
    return con.execute("SELECT 1 FROM games WHERE url = ?", (url,)).fetchone() is not None


def ingest(con: sqlite3.Connection, *, since: str | None = None, months: int | None = None,
           limit: int | None = None, newest_first: bool = False,
           client: ChessComClient | None = None, log: Log = print) -> IngestStats:
    """Fetch archives and store every qualifying blitz game not already present."""
    client = client or ChessComClient()
    stats = IngestStats()
    if since is None and months is None:
        months = config.DEFAULT_MONTHS
    archives = select_archives(client.archives(), since=since, months=months,
                               newest_first=newest_first)
    for url in archives:
        stats.months += 1
        games = client.month_games(url)
        games.sort(key=lambda g: g.get("end_time", 0), reverse=newest_first)
        log(f"[ingest] {year_month(url)}: {len(games)} games")
        for g in games:
            stats.fetched += 1
            if not qualifies(g, stats):
                continue
            if game_exists(con, g["url"]):
                stats.skipped_existing += 1
                continue
            try:
                game_row, positions = parse_game(g, stats=stats)
            except (ValueError, KeyError) as exc:
                stats.skipped_unparseable += 1
                stats.errors.append(f"{g.get('url')}: {exc}")
                continue
            if game_exists(con, game_row["url"]):   # [Link] tag and JSON url can differ
                stats.skipped_existing += 1
                continue
            insert_game(con, game_row, positions)
            stats.inserted += 1
            if limit and stats.inserted >= limit:
                log(f"[ingest] {stats.summary()}")
                return stats
    log(f"[ingest] {stats.summary()}")
    return stats
