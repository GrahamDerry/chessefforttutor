"""A6: build data/fixture.db from a handful of recent games at a reduced depth.

Runs ingest -> analyze -> scenarios -> report against a fresh file, ignoring TUTOR_DB.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from pipeline import config, db
from pipeline.analyze import analyze
from pipeline.engine import Engine
from pipeline.forks import run_forks
from pipeline.ingest import ingest
from pipeline.report import report
from pipeline.scenarios import generate

Log = Callable[[str], None]


def build_fixture(path: Path = config.FIXTURE_DB, *, games: int = config.FIXTURE_GAMES,
                  depth: int = config.FIXTURE_DEPTH, log: Log = print) -> Path:
    path = Path(path)
    for p in (path, path.with_name(path.name + "-journal")):
        p.unlink(missing_ok=True)
    con = db.connect(path)
    try:
        log(f"[fixture] {games} most recent {config.TIME_CLASS} games at depth {depth} -> {path}")
        ingest(con, months=config.DEFAULT_MONTHS, limit=games, newest_first=True, log=log)
        with Engine() as engine:
            analyze(con, depth=depth, engine=engine, log=log)
            run_forks(con, depth=min(depth, config.FORK_DEPTH), engine=engine, log=log)
        generate(con, log=log)
        report(con, out=log)
    finally:
        con.close()
    return path
