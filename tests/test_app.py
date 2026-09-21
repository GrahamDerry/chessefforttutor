"""Bucket B tests: the API must never leak the answer, and must record attempts."""
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app import main as mainmod
from app.explain import explain, why_label


def seed(con: sqlite3.Connection) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    gid = con.execute(
        """INSERT INTO games (url, played_at, time_class, base_seconds, increment, user_color,
               result_user, pgn) VALUES (?,?,?,?,?,?,?,?)""",
        ("https://example.test/1", now, "blitz", 300, 0, "white", 0.0, "[Event \"t\"]"),
    ).lastrowid
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    aid = con.execute(
        """INSERT INTO analysis (fen_key, depth, best_move, shallow_best_move, candidates_json)
           VALUES (?,?,?,?,?)""",
        (" ".join(fen.split(" ")[:4]), 18, "e2e4", "d2d4",
         json.dumps([{"move": "e2e4", "cp": 30, "mate": None, "e": 0.53,
                      "pv": ["e2e4", "e7e5", "g1f3"]}])),
    ).lastrowid

    ids = {}
    for ply, (label, kind, truth, crit) in enumerate(
        [("LONG", "blunder", "LONG", 0.31), ("SHORT", "calm", "SHORT", 0.01),
         ("GRAY", None, None, 0.06)], start=1
    ):
        pid = con.execute(
            """INSERT INTO positions (game_id, ply, fen, user_to_move, phase, move_played,
                   move_san, clock_before, clock_after, seconds_spent, time_fraction,
                   analysis_id, e_best, e_played, e_loss, criticality, obvious, label)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (gid, ply, fen, 1, "opening", "e2e4", "e4", 180.0, 177.9, 2.1, 0.0117,
             aid, 0.55, 0.31, 0.24, crit, 0, label),
        ).lastrowid
        if kind:
            ids[kind] = con.execute(
                """INSERT INTO scenarios (position_id, kind, ground_truth, severity,
                       notes_json, created_at) VALUES (?,?,?,?,?,?)""",
                (pid, kind, truth, 0.24, "{}", now),
            ).lastrowid
    con.commit()
    return ids


@pytest.fixture
def client(tmp_path):
    con = dbmod.init_empty(tmp_path / "t.db")
    ids = seed(con)
    mainmod.set_connection(con)
    c = TestClient(mainmod.app)
    c.ids = ids
    yield c
    mainmod.set_connection(None)
    con.close()


LEAKY = {"ground_truth", "kind", "criticality", "e_loss", "e_best", "label",
         "commitment", "obvious", "severity", "result_user", "move_san",
         "seconds_spent", "best_move_san"}


def test_next_never_leaks_the_answer(client):
    body = client.get("/api/drill/next").json()
    assert LEAKY.isdisjoint(body), f"leaked: {LEAKY & set(body)}"
    assert {"scenario_id", "fen", "user_color", "clock_before"} <= set(body)


def test_next_never_returns_a_gray_position(client):
    # GRAY positions have no scenario row, so they can never be served.
    for _ in range(25):
        sid = client.get("/api/drill/next").json()["scenario_id"]
        assert sid in client.ids.values()


def test_next_respects_exclude(client):
    keep = client.ids["calm"]
    body = client.get(f"/api/drill/next?exclude={client.ids['blunder']}").json()
    assert body["scenario_id"] == keep


def test_next_serves_both_labels_over_time(client):
    truths = set()
    for _ in range(30):
        sid = client.get("/api/drill/next").json()["scenario_id"]
        truths.add("LONG" if sid == client.ids["blunder"] else "SHORT")
    assert truths == {"LONG", "SHORT"}, "drill should mix LONG and SHORT"


def seed_second_game(con: sqlite3.Connection, truth: str = "SHORT") -> int:
    """A second game with one scenario; returns the scenario id."""
    now = datetime.now(timezone.utc).isoformat()
    gid = con.execute(
        """INSERT INTO games (url, played_at, time_class, base_seconds, increment, user_color,
               result_user, pgn) VALUES (?,?,?,?,?,?,?,?)""",
        ("https://example.test/2", now, "blitz", 300, 0, "white", 1.0, "[Event \"t\"]"),
    ).lastrowid
    fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    pid = con.execute(
        """INSERT INTO positions (game_id, ply, fen, user_to_move, phase, move_played,
               move_san, clock_before, clock_after, seconds_spent, time_fraction,
               e_best, e_played, e_loss, criticality, obvious, label)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (gid, 2, fen, 1, "opening", "e7e5", "e5", 180.0, 178.0, 2.0, 0.011,
         0.5, 0.5, 0.0, 0.01, 1, truth),
    ).lastrowid
    sid = con.execute(
        """INSERT INTO scenarios (position_id, kind, ground_truth, severity,
               notes_json, created_at) VALUES (?,?,?,?,?,?)""",
        (pid, "calm" if truth == "SHORT" else "blunder", truth, 0.0, "{}", now),
    ).lastrowid
    con.commit()
    return sid


def test_next_skips_games_already_seen(client):
    """Once a scenario from a game has been served, that game's other scenarios
    are off the table for the rest of the session."""
    other = seed_second_game(mainmod.con())
    for _ in range(30):
        body = client.get(f"/api/drill/next?exclude={client.ids['blunder']}").json()
        assert body["scenario_id"] == other, "served a second position from a seen game"


def test_next_falls_back_when_every_game_is_seen(client):
    """When every game has been used, unseen scenarios from seen games are
    served rather than a 404 -- the drill must not run dry."""
    other = seed_second_game(mainmod.con())
    r = client.get(f"/api/drill/next?exclude={client.ids['blunder']},{other}")
    assert r.status_code == 200
    assert r.json()["scenario_id"] == client.ids["calm"]


def test_answer_records_attempt_and_reveals(client):
    sid = client.ids["blunder"]
    r = client.post("/api/drill/answer",
                    json={"scenario_id": sid, "answer": "LONG", "response_ms": 1200}).json()
    assert r["correct"] is True
    assert r["ground_truth"] == "LONG"
    assert r["move_played_san"] == "e4"
    assert r["best_move_san"] == "e4"
    assert r["pv_san"] == ["e4", "e5", "Nf3"]
    assert r["explanation"]
    assert client.get("/api/stats").json()["attempts"] == 1


def test_answer_marks_wrong_answer_wrong(client):
    r = client.post("/api/drill/answer",
                    json={"scenario_id": client.ids["calm"], "answer": "LONG"}).json()
    assert r["correct"] is False and r["ground_truth"] == "SHORT"


def test_answer_rejects_bad_input(client):
    assert client.post("/api/drill/answer",
                       json={"scenario_id": client.ids["calm"], "answer": "MAYBE"}).status_code == 422
    assert client.post("/api/drill/answer",
                       json={"scenario_id": 9999, "answer": "LONG"}).status_code == 404


def test_stats_breakdowns(client):
    for kind, ans in [("blunder", "LONG"), ("calm", "LONG")]:
        client.post("/api/drill/answer", json={"scenario_id": client.ids[kind], "answer": ans})
    s = client.get("/api/stats").json()
    assert s["attempts"] == 2 and s["accuracy"] == 0.5
    assert s["accuracy_by_kind"]["blunder"]["accuracy"] == 1.0
    assert s["accuracy_by_kind"]["calm"]["accuracy"] == 0.0
    assert s["accuracy_by_phase"]["opening"]["n"] == 2
    assert len(s["recent"]) == 2


def test_scenario_detail(client):
    d = client.get(f"/api/scenarios/{client.ids['blunder']}").json()
    assert d["kind"] == "blunder" and "candidates_json" not in d
    assert d["pv_san"] == ["e4", "e5", "Nf3"]


def test_app_writes_only_drill_attempts(client, tmp_path):
    con = sqlite3.connect(tmp_path / "t.db")
    before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("games", "positions", "analysis", "scenarios")}
    client.post("/api/drill/answer", json={"scenario_id": client.ids["calm"], "answer": "SHORT"})
    after = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after
    con.close()


def test_explanations_are_pure_and_nonempty():
    row = {"ground_truth": "LONG", "criticality": 0.31, "obvious": 0, "commitment": None,
           "seconds_spent": 2.1, "time_fraction": 0.012, "e_loss": 0.24, "move_san": "e4",
           "kind": "blunder"}
    assert "0.31" in why_label(row)
    assert "e4" in explain(row) and explain(row) == explain(row)
    fork = {**row, "commitment": 0.12, "kind": "fork", "e_loss": 0.0}
    assert "fork in the road" in why_label(fork)


def test_next_balances_a_skewed_pool(client):
    """The real scenario pool is mostly SHORT; the drill must still be ~50:50,
    or the user can score well by always answering SHORT."""
    con = mainmod.con()
    pid = con.execute("SELECT position_id FROM scenarios WHERE kind='calm'").fetchone()[0]
    now = datetime.now(timezone.utc).isoformat()
    for k in ("too_much", "lost_advantage"):          # pile on extra SHORT scenarios
        con.execute("""INSERT INTO scenarios (position_id, kind, ground_truth, severity,
                           notes_json, created_at) VALUES (?,?,?,?,?,?)""",
                    (pid, k, "SHORT", 0.0, "{}", now))
    con.commit()
    longs = sum(client.get("/api/drill/next").json()["scenario_id"] == client.ids["blunder"]
                for _ in range(200))
    assert 70 < longs < 130, f"expected roughly half LONG, got {longs}/200"


def test_explanation_has_no_double_spaces():
    row = {"ground_truth": "SHORT", "criticality": 0.02, "obvious": 1, "commitment": None,
           "seconds_spent": 17.4, "time_fraction": 0.14, "e_loss": 0.02,
           "move_san": "Kg2", "kind": "too_much"}
    assert "  " not in explain(row)


def test_next_history_is_the_plies_before_the_position(client):
    """The lead-in replay gets the plies strictly before the drilled one, oldest first."""
    con = mainmod.con()
    pid = con.execute("SELECT id FROM positions WHERE ply = 3").fetchone()[0]
    now = datetime.now(timezone.utc).isoformat()
    sid = con.execute(
        """INSERT INTO scenarios (position_id, kind, ground_truth, severity, notes_json, created_at)
           VALUES (?,?,?,?,?,?)""", (pid, "blunder", "LONG", 0.3, "{}", now)).lastrowid
    con.commit()
    others = ",".join(str(i) for i in client.ids.values())
    body = client.get(f"/api/drill/next?exclude={others}").json()
    assert body["scenario_id"] == sid and body["ply"] == 3
    assert LEAKY.isdisjoint(body)
    assert [h["ply"] for h in body["history"]] == [1, 2]
    for h in body["history"]:
        assert set(h) == {"ply", "fen", "uci", "san"}
        assert h["ply"] < body["ply"]
        assert h["uci"] == "e2e4" and h["san"] == "e4"


def test_next_history_is_empty_at_the_first_ply(client):
    body = client.get(f"/api/drill/next?exclude={client.ids['calm']}").json()
    assert body["scenario_id"] == client.ids["blunder"] and body["ply"] == 1
    assert body["history"] == []
