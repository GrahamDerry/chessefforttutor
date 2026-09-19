"""CLI: python -m pipeline <command> (SPEC.md §4).

    ingest    [--since YYYY-MM] [--months N] [--limit N]
    analyze   [--limit N] [--depth D] [--reshallow]
    forks                                   (A3, not implemented yet)
    scenarios
    report
    fixture   [--games N] [--depth D] [--out PATH]

All commands read TUTOR_DB (default data/tutor.db) except `fixture`, which always writes
data/fixture.db unless --out is given.
"""
from __future__ import annotations

import argparse
import sys

from pipeline import config, db


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="fetch Chess.com archives into games/positions")
    s.add_argument("--since", metavar="YYYY-MM", help="first month to fetch (overrides --months)")
    s.add_argument("--months", type=int, metavar="N", help=f"last N months (default {config.DEFAULT_MONTHS})")
    s.add_argument("--limit", type=int, metavar="N", help="stop after inserting N new games")

    s = sub.add_parser("analyze", help="run Stockfish over every unscored game")
    s.add_argument("--limit", type=int, metavar="N", help="analyze at most N games")
    s.add_argument("--depth", type=int, default=config.ANALYSIS_DEPTH, metavar="D")
    s.add_argument("--workers", type=int, default=config.WORKERS, metavar="N",
                   help="parallel engine processes, one game each (default %(default)s)")
    s.add_argument("--reshallow", action="store_true",
                   help="only refresh shallow_best_move/obvious/label at the current SHALLOW_DEPTH")

    sub.add_parser("forks", help="commitment / fork pass (A3)")
    sub.add_parser("scenarios", help="regenerate the scenarios table")
    sub.add_parser("report", help="print the tuning report")

    s = sub.add_parser("fixture", help=f"build {config.FIXTURE_DB.name} from a few recent games")
    s.add_argument("--games", type=int, default=config.FIXTURE_GAMES, metavar="N")
    s.add_argument("--depth", type=int, default=config.FIXTURE_DEPTH, metavar="D")
    s.add_argument("--out", default=str(config.FIXTURE_DB), metavar="PATH")

    args = p.parse_args(argv)

    if args.cmd == "fixture":
        from pipeline.fixture import build_fixture
        build_fixture(args.out, games=args.games, depth=args.depth)
        return 0

    if args.cmd == "forks":
        print("forks: not implemented yet (A3). Positions keep the labels from `analyze`.",
              file=sys.stderr)
        return 2

    con = db.connect()
    try:
        if args.cmd == "ingest":
            from pipeline.ingest import ingest
            ingest(con, since=args.since, months=args.months, limit=args.limit)
        elif args.cmd == "analyze":
            from pipeline.analyze import analyze, reshallow
            if args.reshallow:
                reshallow(con)
            else:
                analyze(con, depth=args.depth, limit=args.limit, workers=args.workers)
        elif args.cmd == "scenarios":
            from pipeline.scenarios import generate
            generate(con)
        elif args.cmd == "report":
            from pipeline.report import report
            report(con)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
