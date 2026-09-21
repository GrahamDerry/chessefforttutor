-- Canonical schema for data/tutor.db. Single source of truth (see SPEC.md §3).
-- Schema changes go together with an update to SPEC.md §3.
-- pipeline/ writes every table; app/ writes only drill_attempts.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS games (
  id              INTEGER PRIMARY KEY,
  url             TEXT UNIQUE NOT NULL,       -- [Link] tag
  played_at       TEXT NOT NULL,              -- ISO 8601 UTC
  time_class      TEXT NOT NULL,              -- 'blitz'
  base_seconds    INTEGER NOT NULL,
  increment       INTEGER NOT NULL DEFAULT 0,
  user_color      TEXT NOT NULL CHECK (user_color IN ('white','black')),
  user_rating     INTEGER,
  opp_name        TEXT,
  opp_rating      INTEGER,
  result_user     REAL NOT NULL CHECK (result_user IN (1.0, 0.5, 0.0)),
  eco             TEXT,
  eco_url         TEXT,
  termination     TEXT,
  pgn             TEXT NOT NULL,
  analyzed_at     TEXT,                       -- NULL until every ply is scored
  analysis_depth  INTEGER
);

-- Engine cache keyed by POSITION, not game. fen_key = FEN minus halfmove/fullmove counters.
CREATE TABLE IF NOT EXISTS analysis (
  id                INTEGER PRIMARY KEY,
  fen_key           TEXT NOT NULL,
  depth             INTEGER NOT NULL,
  best_move         TEXT NOT NULL,            -- UCI
  shallow_best_move TEXT NOT NULL,            -- UCI, at SHALLOW_DEPTH
  candidates_json   TEXT NOT NULL,            -- [{"move","cp","mate","e","pv":[...]}, ...] up to 4
  UNIQUE (fen_key, depth)
);

-- One row per ply: the decision point BEFORE the move.
CREATE TABLE IF NOT EXISTS positions (
  id             INTEGER PRIMARY KEY,
  game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  ply            INTEGER NOT NULL,            -- 1 = White's first move
  fen            TEXT NOT NULL,
  user_to_move   INTEGER NOT NULL CHECK (user_to_move IN (0,1)),
  phase          TEXT CHECK (phase IN ('opening','middlegame','endgame')),
  move_played    TEXT NOT NULL,               -- UCI
  move_san       TEXT NOT NULL,
  clock_before   REAL,                        -- seconds; NULL if no %clk data
  clock_after    REAL,
  seconds_spent  REAL,
  time_fraction  REAL,
  analysis_id    INTEGER REFERENCES analysis(id),
  e_best         REAL,
  e_played       REAL,
  e_loss         REAL,
  criticality    REAL,
  obvious        INTEGER CHECK (obvious IN (0,1)),
  commitment     REAL,                        -- NULL unless fork pass ran
  label          TEXT CHECK (label IN ('LONG','SHORT','GRAY')),
  UNIQUE (game_id, ply)
);

CREATE TABLE IF NOT EXISTS scenarios (
  id            INTEGER PRIMARY KEY,
  position_id   INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL CHECK (kind IN ('blunder','too_little','too_much','lost_advantage','fork','calm')),
  ground_truth  TEXT NOT NULL CHECK (ground_truth IN ('LONG','SHORT')),
  severity      REAL,
  notes_json    TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE (position_id, kind)
);

CREATE TABLE IF NOT EXISTS drill_attempts (
  id           INTEGER PRIMARY KEY,
  scenario_id  INTEGER NOT NULL REFERENCES scenarios(id),
  answer       TEXT NOT NULL CHECK (answer IN ('LONG','SHORT')),
  correct      INTEGER NOT NULL CHECK (correct IN (0,1)),
  response_ms  INTEGER,
  answered_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_game     ON positions(game_id);
CREATE INDEX IF NOT EXISTS idx_positions_label    ON positions(label) WHERE user_to_move = 1;
CREATE INDEX IF NOT EXISTS idx_analysis_fen       ON analysis(fen_key);
CREATE INDEX IF NOT EXISTS idx_scenarios_truth    ON scenarios(ground_truth);
CREATE INDEX IF NOT EXISTS idx_attempts_scenario  ON drill_attempts(scenario_id);
