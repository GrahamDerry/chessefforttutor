"""Bucket A tests (SPEC.md §4): pure metrics, scoring, scenario invariants. No engine needed."""
import json
import math
import sqlite3

import chess
import pytest

from pipeline import config
from pipeline.analyze import score_positions
from pipeline.chesscom import select_archives
from pipeline.engine import AnalysisResult
from pipeline.metrics import (E, criticality, fen_key, label, parse_clock, parse_time_control,
                              percentile, phase, result_for, seconds_spent, time_fraction)
from pipeline.scenarios import desired_scenarios


# --------------------------------------------------------------------------- E(cp)
def test_expected_points_basics():
    assert E(0) == 0.5
    assert math.isclose(E(100), 1 / (1 + math.exp(-0.368)))
    assert math.isclose(E(100) + E(-100), 1.0)
    assert E(None, mate=3) == 1.0
    assert E(None, mate=-1) == 0.0
    assert E(None, mate=0) == 0.0          # mover is already checkmated
    with pytest.raises(ValueError):
        E(None)


# --------------------------------------------------------------------------- clocks
def test_parse_clock_with_tenths():
    assert parse_clock("0:04:57.6") == pytest.approx(297.6)
    assert parse_clock("0:04:38") == pytest.approx(278.0)
    assert parse_clock("1:00:00") == 3600.0


def test_seconds_spent_adds_increment_back():
    # 300+5: 297.0 -> 299.5 means 2.5 s were spent, not -2.5.
    assert seconds_spent(297.0, 299.5, 5) == pytest.approx(2.5)
    assert seconds_spent(300.0, 297.6, 0) == pytest.approx(2.4)
    assert seconds_spent(10.0, 10.1, 0) == 0.0       # lag compensation clamps at zero
    assert time_fraction(3.0, 300.0) == pytest.approx(0.01)
    assert time_fraction(3.0, 0.0) is None


def test_parse_time_control():
    assert parse_time_control("300") == (300, 0)
    assert parse_time_control("300+5") == (300, 5)
    assert parse_time_control("180") == (180, 0)
    with pytest.raises(ValueError):
        parse_time_control("1/86400")


# --------------------------------------------------------------------------- criticality / label
def cands(*es):
    return [{"move": f"m{i}", "e": e} for i, e in enumerate(es)]


def test_criticality_from_candidates_json():
    c = json.loads(json.dumps(cands(0.60, 0.50, 0.45, 0.40)))
    assert criticality(c) == pytest.approx(0.60 - (0.50 + 0.45 + 0.40) / 3)
    assert criticality(cands(0.60, 0.50)) == pytest.approx(0.10)     # fewer moves: use what exists
    assert criticality(cands(0.60)) == 0.0                           # one legal move
    assert criticality(cands(1.0, 1.0, 0.7)) == pytest.approx(0.15)  # mate scores are just E=1


@pytest.mark.parametrize("crit,obvious,fork,expected", [
    (config.CRIT, False, False, "LONG"),
    (config.CRIT + 0.2, False, False, "LONG"),
    (config.CRIT + 0.2, True, False, "SHORT"),          # obvious beats criticality
    (config.CALM, False, False, "SHORT"),
    (0.0, False, False, "SHORT"),
    ((config.CALM + config.CRIT) / 2, False, False, "GRAY"),
    ((config.CALM + config.CRIT) / 2, True, False, "SHORT"),
    (0.0, True, True, "LONG"),                          # fork always LONG
    (config.CALM, False, True, "LONG"),
])
def test_label_truth_table(crit, obvious, fork, expected):
    assert label(crit, obvious, fork) == expected


# --------------------------------------------------------------------------- fen_key
def test_fen_key_strips_counters_and_unusable_ep():
    b = chess.Board()
    assert fen_key(b) == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
    assert fen_key("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 5 40") == fen_key(b)
    b.push_san("e4")                     # no black pawn can capture on e3 -> no ep square
    assert fen_key(b).endswith(" b KQkq -")
    b.push_san("d5"); b.push_san("e5"); b.push_san("f5")   # exf6 is legal -> ep square kept
    assert fen_key(b).endswith(" w KQkq f6")


# --------------------------------------------------------------------------- phase / result / misc
def test_phase_rules():
    b = chess.Board()
    assert phase(b, 24) == "opening"
    assert phase(b, 25) == "middlegame"
    assert phase(chess.Board("4k3/8/8/8/8/8/8/R3K2R w KQ - 0 1"), 60) == "endgame"   # no queens
    assert phase(chess.Board("4k3/8/8/8/8/8/8/Q3K3 w - - 0 1"), 60) == "endgame"      # Q = 9 <= 13
    assert phase(chess.Board("r3k3/8/8/8/8/8/8/Q3K2R w K - 0 1"), 60) == "middlegame" # 9+5+5 = 19
    assert phase(chess.Board("4k3/8/8/8/8/8/8/Q3K2R w K - 0 1"), 60) == "middlegame"  # 9+5 = 14 > 13


def test_result_for_user():
    assert result_for("1-0", "white") == 1.0
    assert result_for("1-0", "black") == 0.0
    assert result_for("0-1", "black") == 1.0
    assert result_for("1/2-1/2", "white") == 0.5
    with pytest.raises(ValueError):
        result_for("*", "white")


def test_percentile_and_archive_selection():
    assert percentile([5, 1, 3, 2, 4], 25) == 2
    assert percentile([], 25) is None
    arcs = [f"https://api.chess.com/pub/player/reitsy/games/2026/{m:02d}" for m in range(1, 10)]
    assert select_archives(arcs, months=2) == arcs[-2:]
    assert select_archives(arcs, since="2026-08") == arcs[-2:]
    assert select_archives(arcs, months=3, newest_first=True)[0].endswith("/09")


# --------------------------------------------------------------------------- ingest (offline, Chess.com-shaped)
CHESSCOM_GAME = {
    "url": "https://www.chess.com/game/live/1", "time_class": "blitz", "time_control": "180+2",
    "rules": "chess", "end_time": 1789000000,
    "white": {"username": "Someone", "rating": 2200}, "black": {"username": "reitsy", "rating": 2300},
    "pgn": '[Event "Live Chess"]\n[Result "0-1"]\n[UTCDate "2026.09.01"]\n[UTCTime "10:00:00"]\n'
           '[ECO "B00"]\n[TimeControl "180+2"]\n[Termination "Reitsy won by resignation"]\n'
           '[Link "https://www.chess.com/game/live/1"]\n\n'
           '1. e4 {[%clk 0:03:01.5]} 1... e5 {[%clk 0:02:59.9]} 2. Nf3 {[%clk 0:03:02.1]} '
           '2... Nc6 {[%clk 0:02:51.3]} 0-1',
}


def test_parse_game_clocks_with_increment_and_tenths():
    from pipeline.ingest import parse_game
    game, pos = parse_game(CHESSCOM_GAME, username="Reitsy")
    assert game["user_color"] == "black" and game["result_user"] == 1.0
    assert (game["base_seconds"], game["increment"]) == (180, 2)
    assert game["played_at"] == "2026-09-01T10:00:00Z" and game["opp_name"] == "Someone"
    assert [p["ply"] for p in pos] == [1, 2, 3, 4]
    # White's first move: 180 -> 181.5 after +2 increment means 0.5 s spent, not -1.5.
    assert pos[0]["clock_before"] == 180.0 and pos[0]["seconds_spent"] == pytest.approx(0.5)
    # Black's first move compares against base too: 180 -> 179.9 (+2) = 2.1 s.
    assert pos[1]["user_to_move"] == 1 and pos[1]["seconds_spent"] == pytest.approx(2.1)
    # Later moves compare against the same side's previous clock: 181.5 -> 182.1 (+2) = 1.4 s.
    assert pos[2]["clock_before"] == pytest.approx(181.5) and pos[2]["seconds_spent"] == pytest.approx(1.4)
    assert pos[3]["seconds_spent"] == pytest.approx(10.6)
    assert pos[3]["time_fraction"] == pytest.approx(10.6 / 179.9, rel=1e-3)
    assert pos[3]["move_san"] == "Nc6" and pos[3]["move_played"] == "b8c6"


def test_ingest_into_db_is_idempotent(tmp_path):
    from pipeline import db
    from pipeline.ingest import ingest

    class FakeClient:
        def archives(self):
            return ["https://api.chess.com/pub/player/reitsy/games/2026/09"]

        def month_games(self, url):
            bullet = dict(CHESSCOM_GAME, url="https://www.chess.com/game/live/2", time_class="bullet")
            noclock = dict(CHESSCOM_GAME, url="https://www.chess.com/game/live/3",
                           pgn=CHESSCOM_GAME["pgn"].replace("{[%clk 0:03:01.5]} ", "").replace("%clk", "x"))
            return [CHESSCOM_GAME, bullet, noclock]

    con = db.connect(tmp_path / "t.db")
    s1 = ingest(con, months=1, client=FakeClient(), log=lambda m: None)
    assert (s1.inserted, s1.skipped_time_class, s1.skipped_no_clock) == (1, 1, 1)
    s2 = ingest(con, months=1, client=FakeClient(), log=lambda m: None)
    assert s2.inserted == 0 and s2.skipped_existing == 1
    assert con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 4


# --------------------------------------------------------------------------- scoring (e_played from next ply)
def _res(e_best, e_rest, best="a1a2", shallow="a1a2", id_=1):
    es = [e_best] + list(e_rest)
    return AnalysisResult("k", 12, best, shallow,
                          [{"move": f"m{i}", "cp": 0, "mate": None, "e": e, "pv": []} for i, e in enumerate(es)],
                          id=id_)


START = chess.STARTING_FEN


def _pos(id_, commitment=None, fen=START):
    return {"id": id_, "commitment": commitment, "fen": fen}


def test_score_positions_flips_perspective_for_e_played():
    # Ply 1 (user): best 0.60. Ply 2 (opponent to move): best 0.55 from THEIR side,
    # so the user's move yielded 0.45 -> loss 0.15. Last ply's next is the terminal value.
    results = [_res(0.60, [0.40, 0.40, 0.40], id_=1),
               _res(0.55, [0.55, 0.55, 0.54], best="b", shallow="c", id_=2)]
    rows = score_positions([_pos(1), _pos(2)], results, e_best_after_last=0.30)
    assert rows[0]["e_played"] == pytest.approx(0.45)
    assert rows[0]["e_loss"] == pytest.approx(0.15)
    assert rows[0]["criticality"] == pytest.approx(0.20)
    assert rows[0]["obvious"] == 1 and rows[0]["label"] == "SHORT"       # obvious -> SHORT
    assert rows[1]["e_played"] == pytest.approx(0.70)
    assert rows[1]["e_loss"] == 0.0                                       # gains are clamped
    assert rows[1]["obvious"] == 0 and rows[1]["label"] == "SHORT"       # C = 0.005 <= CALM
    assert rows[1]["analysis_id"] == 2


def test_score_positions_leaves_lite_rows_unlabelled():
    # An opponent ply searched with one line still yields e_best/e_played but no label.
    lite = _res(0.55, [])
    rows = score_positions([_pos(1)], [lite], e_best_after_last=0.40)
    assert rows[0]["e_played"] == pytest.approx(0.60) and rows[0]["e_loss"] == 0.0
    assert rows[0]["criticality"] is None and rows[0]["label"] is None
    # ...but a position with a single legal move is "full" with one line (C = 0 -> SHORT).
    one_move = "k7/1p6/8/8/8/8/8/R6K b - - 0 1"     # Ka8 in check, only Kb8
    assert chess.Board(one_move).legal_moves.count() == 1
    rows = score_positions([_pos(1, fen=one_move)], [_res(0.0, [])], 1.0)
    assert rows[0]["label"] == "SHORT" and rows[0]["criticality"] == 0.0


def test_score_positions_keeps_fork_label():
    results = [_res(0.5, [0.5, 0.5, 0.5], best="b", shallow="c")]
    assert score_positions([_pos(1, commitment=config.FORK)], results, 0.5)[0]["label"] == "LONG"
    assert score_positions([_pos(1, commitment=config.FORK - 0.01)], results, 0.5)[0]["label"] == "SHORT"


# --------------------------------------------------------------------------- forks (pure parts)
def test_fork_gate_and_irreversibility():
    from pipeline.forks import (commitment_from_sharpness, is_irreversible, near_equal,
                                opens_with_trade, passes_gate, user_decision_points)
    b = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    c = [{"move": "f1b5", "e": 0.518, "pv": ["f1b5", "g8f6"]},
         {"move": "f1c4", "e": 0.509, "pv": ["f1c4", "g8f6"]},
         {"move": "d2d4", "e": 0.509, "pv": ["d2d4", "e5d4", "f3d4"]},
         {"move": "b1c3", "e": 0.470, "pv": ["b1c3"]}]
    assert [x["move"] for x in near_equal(c)] == ["f1b5", "f1c4", "d2d4"]   # b1c3 is 0.048 away
    assert not is_irreversible(b, c[0]) and not is_irreversible(b, c[1])
    assert is_irreversible(b, c[2])                                          # pawn move
    assert [x["move"] for x in passes_gate(b, 0.01, c)] == ["f1b5", "f1c4", "d2d4"]
    assert passes_gate(b, config.CALM + 0.001, c) == []                      # too critical
    assert passes_gate(b, 0.0, c[:2]) == []                                  # nothing irreversible
    # trade: Nxe5 Nxe5 recaptures on the same square
    t = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    assert opens_with_trade(t, ["f3e5", "c6e5"])
    assert not opens_with_trade(t, ["f3e5", "g8f6"])
    # decision points: after 2, 4, 6 plies; stops at PV end
    pts = user_decision_points(b, ["f1b5", "g8f6", "e1g1", "f8c5", "b1c3", "d7d6", "d2d3"])
    assert len(pts) == 3 and all(p.turn == chess.WHITE for p in pts)
    assert len(user_decision_points(b, ["f1b5", "g8f6", "e1g1"])) == 1
    assert commitment_from_sharpness([0.02, 0.11, 0.05]) == pytest.approx(0.09)
    assert commitment_from_sharpness([0.02]) is None


# --------------------------------------------------------------------------- scenarios
def _row(**kw):
    base = dict(id=0, game_id=1, ply=30, user_to_move=1, label="SHORT", e_best=0.5, e_played=0.5,
                e_loss=0.0, seconds_spent=5.0, time_fraction=0.02, clock_before=200.0,
                commitment=None, base_seconds=300, increment=0, result_user=0.0)
    base.update(kw)
    return base


def test_ground_truth_is_the_label_not_the_kind():
    rows = [
        _row(id=1, label="SHORT", e_loss=0.35, time_fraction=0.01),        # blunder in a SHORT position
        _row(id=2, label="LONG", e_loss=0.25, seconds_spent=0.5),          # blunder + too_little
        _row(id=3, label="GRAY", e_loss=0.50),                             # never a scenario
        _row(id=4, label="SHORT", time_fraction=0.30),                     # too_much
        _row(id=5, label="SHORT", commitment=0.09),                        # fork
    ] + [_row(id=10 + i, seconds_spent=8.0 + i) for i in range(20)]        # calm pool / pace
    want = desired_scenarios(rows, log=lambda s: None)
    assert want[(1, "blunder")][0] == "SHORT"
    assert want[(2, "blunder")][0] == "LONG"
    assert want[(2, "too_little")][0] == "LONG"
    assert want[(4, "too_much")][0] == "SHORT"
    assert want[(5, "fork")][0] == "SHORT" and want[(5, "fork")][1] == 0.09
    assert not any(pid == 3 for pid, _ in want)
    assert all(truth in ("LONG", "SHORT") for truth, _, _ in want.values())
    calm = [k for k in want if k[1] == "calm"]
    assert len(calm) >= config.CALM_MIN
    assert all(want[k][0] == "SHORT" for k in calm)


def test_lost_advantage_picks_biggest_drop_after_peak():
    g = [
        _row(id=1, ply=1, user_to_move=1, e_best=0.60, e_loss=0.0),
        _row(id=2, ply=2, user_to_move=0, e_best=0.20),                     # user E = 0.80 (peak)
        _row(id=3, ply=3, user_to_move=1, e_best=0.80, e_loss=0.05),
        _row(id=4, ply=4, user_to_move=0, e_best=0.25),
        _row(id=5, ply=5, user_to_move=1, label="LONG", e_best=0.75, e_loss=0.30),  # the drop
    ]
    want = desired_scenarios(g, log=lambda s: None)
    truth, sev, notes = want[(5, "lost_advantage")]
    assert truth == "LONG" and sev == 0.30 and notes["peak_ply"] == 2 and notes["peak_e"] == 0.8
    won = [dict(r, result_user=1.0) for r in g]
    assert not any(k == "lost_advantage" for _, k in desired_scenarios(won, log=lambda s: None))


def test_analysis_cache_commits_immediately_so_workers_do_not_block(tmp_path):
    """Regression: with --workers N, a cache write left uncommitted held the SQLite write
    lock for a whole game and starved the other workers past their busy timeout."""
    from pipeline import db
    from pipeline.engine import AnalysisCache

    path = tmp_path / "t.db"
    a, b = db.connect(path), db.connect(path)
    b.execute("PRAGMA busy_timeout = 200")   # fail fast instead of waiting on a held lock
    cache_a = AnalysisCache(a)
    cache_a.put(AnalysisResult(fen_key="k1", depth=18, best_move="e2e4", shallow_best_move="e2e4",
                               candidates=[{"move": "e2e4", "e": 0.5}]))
    # If put() had not committed, this write would raise "database is locked".
    AnalysisCache(b).put(AnalysisResult(fen_key="k2", depth=18, best_move="d2d4", shallow_best_move="d2d4",
                                        candidates=[{"move": "d2d4", "e": 0.5}]))
    assert a.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 2
    a.close(); b.close()
