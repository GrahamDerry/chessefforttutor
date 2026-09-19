"""Generate a fake tutor.db so Bucket B can build before Bucket A's fixture lands.

Real FENs and real legal moves (so SAN conversion is exercised), fabricated evals.
Delete this once data/fixture.db exists.

    python tools/make_fake_db.py data/fake.db
"""
import json, math, random, sqlite3, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "pipeline" / "schema.sql"
DEPTH = 18


def E(cp: float) -> float:
    return 1 / (1 + math.exp(-0.00368 * cp))


def fen_key(board: chess.Board) -> str:
    return " ".join(board.fen().split(" ")[:4])


def phase_of(board: chess.Board, ply: int) -> str:
    if ply <= 24:
        return "opening"
    if not board.pieces(chess.QUEEN, chess.WHITE) and not board.pieces(chess.QUEEN, chess.BLACK):
        return "endgame"
    return "middlegame"


def build(path: Path, n_games: int = 6, seed: int = 7) -> None:
    rng = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())

    now = datetime.now(timezone.utc)
    scenario_rows: list[tuple] = []

    for g in range(n_games):
        user_color = "white" if g % 2 == 0 else "black"
        base, inc = (300, 0) if g % 3 else (180, 2)
        result = rng.choice([1.0, 0.5, 0.0])
        url = f"https://www.chess.com/game/live/90000000{g:02d}"
        game_id = con.execute(
            """INSERT INTO games (url, played_at, time_class, base_seconds, increment,
                   user_color, user_rating, opp_name, opp_rating, result_user, eco, eco_url,
                   termination, pgn, analyzed_at, analysis_depth)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (url, (now - timedelta(days=g)).isoformat(), "blitz", base, inc, user_color,
             2300 + rng.randint(-40, 40), f"fakeopponent{g}", 2300 + rng.randint(-80, 80),
             result, "C02", "https://www.chess.com/openings/French-Defense",
             "fake game — not a real result", "[Event \"Fake\"]\n\n1. e4 *", now.isoformat(), DEPTH),
        ).lastrowid

        board = chess.Board()
        clock = {chess.WHITE: float(base), chess.BLACK: float(base)}
        plies = rng.randint(34, 50)

        for ply in range(1, plies + 1):
            legal = list(board.legal_moves)
            if not legal or board.is_game_over():
                break
            mover_is_user = (board.turn == chess.WHITE) == (user_color == "white")

            # Fabricated engine view: top-4 legal moves with descending evals.
            cands = rng.sample(legal, min(4, len(legal)))
            spread = rng.choice([5, 15, 40, 120, 300])  # cp gap driving criticality
            cps = [rng.randint(-250, 250)]
            for i in range(1, len(cands)):
                cps.append(cps[0] - spread * i - rng.randint(0, 20))
            cand_json = json.dumps([
                {"move": m.uci(), "cp": cp, "mate": None, "e": round(E(cp), 4),
                 "pv": [m.uci()]}
                for m, cp in zip(cands, cps)
            ])
            best = cands[0]
            shallow = best if rng.random() < 0.65 else rng.choice(legal)
            analysis_id = con.execute(
                """INSERT OR IGNORE INTO analysis
                   (fen_key, depth, best_move, shallow_best_move, candidates_json)
                   VALUES (?,?,?,?,?)""",
                (fen_key(board), DEPTH, best.uci(), shallow.uci(), cand_json),
            ).lastrowid or con.execute(
                "SELECT id FROM analysis WHERE fen_key=? AND depth=?", (fen_key(board), DEPTH)
            ).fetchone()[0]

            e_best = E(cps[0])
            crit = e_best - sum(E(c) for c in cps[1:]) / max(1, len(cps) - 1)
            obvious = int(shallow == best)
            fork = crit <= 0.03 and rng.random() < 0.12
            commitment = round(rng.uniform(0.08, 0.20), 4) if fork else None
            if fork or (crit >= 0.10 and not obvious):
                label = "LONG"
            elif crit <= 0.03 or obvious:
                label = "SHORT"
            else:
                label = "GRAY"

            played = best if rng.random() < 0.7 else rng.choice(legal)
            e_played = e_best - (0.0 if played == best else rng.uniform(0.0, 0.35))
            e_loss = round(e_best - e_played, 4)

            clock_before = clock[board.turn]
            spent = min(clock_before - 0.5, max(0.1, rng.expovariate(1 / 6.0)))
            clock_after = clock_before - spent + inc
            clock[board.turn] = clock_after

            san = board.san(played)
            pos_id = con.execute(
                """INSERT INTO positions (game_id, ply, fen, user_to_move, phase, move_played,
                       move_san, clock_before, clock_after, seconds_spent, time_fraction,
                       analysis_id, e_best, e_played, e_loss, criticality, obvious, commitment, label)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (game_id, ply, board.fen(), int(mover_is_user), phase_of(board, ply),
                 played.uci(), san, round(clock_before, 1), round(clock_after, 1),
                 round(spent, 1), round(spent / clock_before, 4), analysis_id,
                 round(e_best, 4), round(e_played, 4), e_loss, round(crit, 4), obvious,
                 commitment, label),
            ).lastrowid

            if mover_is_user and label != "GRAY":
                kind = None
                if e_loss >= 0.20:
                    kind = "blunder"
                elif fork:
                    kind = "fork"
                elif label == "LONG" and e_loss >= 0.10 and spent < 3:
                    kind = "too_little"
                elif label == "SHORT" and spent / clock_before >= 0.10:
                    kind = "too_much"
                elif label == "SHORT" and rng.random() < 0.5:
                    kind = "calm"
                if kind:
                    # ground_truth is the POSITION's label, never derived from kind:
                    # you can blunder in a position that deserved a fast move.
                    scenario_rows.append((pos_id, kind, label,
                                          commitment if kind == "fork" else e_loss,
                                          json.dumps({"fake": True}), now.isoformat()))
            board.push(played)

    con.executemany(
        """INSERT OR IGNORE INTO scenarios
           (position_id, kind, ground_truth, severity, notes_json, created_at)
           VALUES (?,?,?,?,?,?)""", scenario_rows)
    con.commit()

    counts = dict(con.execute("SELECT ground_truth, COUNT(*) FROM scenarios GROUP BY 1").fetchall())
    kinds = dict(con.execute("SELECT kind, COUNT(*) FROM scenarios GROUP BY 1").fetchall())
    print(f"wrote {path}")
    print(f"  games={con.execute('SELECT COUNT(*) FROM games').fetchone()[0]}"
          f" positions={con.execute('SELECT COUNT(*) FROM positions').fetchone()[0]}"
          f" scenarios={sum(counts.values())}")
    print(f"  ground_truth={counts}")
    print(f"  kinds={kinds}")
    con.close()


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "data" / "fake.db"))
