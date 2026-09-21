# Chess Effort Tutor — Design

A personal chess tutor that mines a Chess.com player's blitz games for the moments
where time was misallocated, then drills one decision per position:
**think long, or think short?**

This document holds the definitions, the schema, and a description of each component.
For how to run the program, see [README.md](README.md).

Every existing puzzle tool asks "what's the best move?" — and by being a puzzle, it
tells you the position is critical. Real games never do. This app trains the triage
skill directly, using positions from your own games, and shows you afterwards what
you actually did on the clock.

---

## 1. Design decisions

| Topic | Decision |
|---|---|
| Player | Any Chess.com user, set by `CHESSCOM_USER`. Thresholds were tuned on the author's account (~2300 blitz, ≈500 blitz games per 6 months, mostly `300`, some `180` and `300+5`). |
| Game scope (v1) | `time_class == "blitz"` only. Ignore bullet/rapid/daily. |
| Engine | Stockfish 19 (`brew install stockfish`, at `/opt/homebrew/bin/stockfish`). Engine is ground truth; an LLM may *explain* but never *evaluate*. |
| Stack | Python 3.14, `python-chess`, SQLite, FastAPI + uvicorn, plain JS frontend (chessboard rendered with `chessground` or `chessboard.js` via CDN). |
| Runtime | Local only. No hosting, no auth. |
| Drill mode (v1) | Triage only: user answers LONG / SHORT. No "find the move" step. |
| Units | **Expected points** (0–1), never raw centipawns, for every loss/threshold. |

---

## 2. Core definitions (shared vocabulary — do not redefine locally)

All values are from the **mover's** point of view at the decision point (the position
*before* the move is played).

```
E(cp)   = 1 / (1 + exp(-0.00368 * cp))          # expected points; mate → 1.0 or 0.0
```

**Criticality** — how much a decent-looking move loses on average vs. the best move.
From one `MultiPV=4` search at `ANALYSIS_DEPTH` (default 18):

```
C = E(best) − mean(E(2nd), E(3rd), E(4th))       # fewer legal moves → use what exists; 1 legal move → C = 0
```

**Obviousness** — does fast intuition already find the right move? Proxy: a shallow
search agrees with the deep one.

```
obvious = best_move(depth SHALLOW_DEPTH) == best_move(depth ANALYSIS_DEPTH)   # SHALLOW_DEPTH default 4
```

**Commitment** (fork detection) — only computed when `C ≤ CALM` and ≥2 candidates are
within 0.03 E of best and at least one of them is *irreversible* (pawn move, capture,
castling, or PV opens with a trade). For each such candidate, follow its PV 6 plies and
compute C at each of the user's decision points; `sharpness(m) = mean(C)`.

```
commitment = max(sharpness) − min(sharpness)     # over the near-equal candidates
fork       = commitment ≥ FORK (default 0.08)
```

**Ground truth label** (depends only on the position):

```
LONG   if (C ≥ CRIT and not obvious) or fork          # CRIT default 0.10
SHORT  if (C ≤ CALM and not fork) or (obvious and not fork)   # CALM default 0.03
GRAY   otherwise → never shown in the drill
```

**Time** (clock tags are `[%clk H:MM:SS(.t)]`, remaining time *after* the move):

```
seconds_spent = clock_before − clock_after + increment
time_fraction = seconds_spent / clock_before
```

**Move loss** — no extra search needed: the position after the user's move is the next
ply, already analyzed. Flip perspective:

```
e_played = 1 − e_best(next ply)
e_loss   = e_best − e_played
```

**Scenario kinds** (depend on what the user *did*; separate from the label):

| kind | definition |
|---|---|
| `blunder` | user move with `e_loss ≥ 0.20` |
| `too_little` | label LONG, `e_loss ≥ 0.10`, `seconds_spent` < user's 25th percentile for that time control |
| `too_much` | label SHORT, `time_fraction ≥ 0.10` |
| `lost_advantage` | user's E peaked ≥ 0.75, result ≤ 0.5; the user move with the largest E drop after the peak |
| `fork` | `fork == true` |
| `calm` | label SHORT, sampled to balance the drill (see §5) |

All thresholds live in one place: `pipeline/config.py`. They were tuned against a
2300-rated player's games using `python -m pipeline report`; for a different strength,
`SHALLOW_DEPTH` and `CRIT` are the first two to revisit.

---

## 3. The contract: SQLite schema

File: `data/tutor.db` (or wherever `TUTOR_DB` points). `pipeline/` writes every table;
`app/` reads everything and writes only `drill_attempts`. Canonical DDL lives in
`pipeline/schema.sql` and is the single source of truth; the copy below is documentation
and should be updated in the same change.

```sql
CREATE TABLE games (
  id              INTEGER PRIMARY KEY,
  url             TEXT UNIQUE NOT NULL,       -- [Link] tag, e.g. https://www.chess.com/game/live/184020340938
  played_at       TEXT NOT NULL,              -- ISO 8601 UTC
  time_class      TEXT NOT NULL,              -- 'blitz'
  base_seconds    INTEGER NOT NULL,
  increment       INTEGER NOT NULL DEFAULT 0,
  user_color      TEXT NOT NULL,              -- 'white' | 'black'
  user_rating     INTEGER,
  opp_name        TEXT,
  opp_rating      INTEGER,
  result_user     REAL NOT NULL,              -- 1.0 | 0.5 | 0.0
  eco             TEXT,
  eco_url         TEXT,
  termination     TEXT,
  pgn             TEXT NOT NULL,
  analyzed_at     TEXT,                       -- NULL until pipeline has scored every ply
  analysis_depth  INTEGER
);

CREATE TABLE analysis (                       -- engine cache, keyed by POSITION not game
  id               INTEGER PRIMARY KEY,
  fen_key          TEXT NOT NULL,             -- FEN with halfmove/fullmove counters stripped
  depth            INTEGER NOT NULL,
  best_move        TEXT NOT NULL,             -- UCI
  shallow_best_move TEXT NOT NULL,            -- UCI, at SHALLOW_DEPTH
  candidates_json  TEXT NOT NULL,             -- [{"move":"e2e4","cp":31,"mate":null,"e":0.53,"pv":["e2e4","e7e5",...]}, ...] up to 4
  UNIQUE (fen_key, depth)
);

CREATE TABLE positions (                      -- one row per ply; the decision point BEFORE the move
  id             INTEGER PRIMARY KEY,
  game_id        INTEGER NOT NULL REFERENCES games(id),
  ply            INTEGER NOT NULL,            -- 1 = White's first move
  fen            TEXT NOT NULL,
  user_to_move   INTEGER NOT NULL,            -- 0/1
  phase          TEXT,                        -- 'opening' | 'middlegame' | 'endgame'
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
  obvious        INTEGER,                     -- 0/1
  commitment     REAL,                        -- NULL unless computed
  label          TEXT,                        -- 'LONG' | 'SHORT' | 'GRAY'
  UNIQUE (game_id, ply)
);

CREATE TABLE scenarios (
  id            INTEGER PRIMARY KEY,
  position_id   INTEGER NOT NULL REFERENCES positions(id),
  kind          TEXT NOT NULL,                -- see §2 table
  ground_truth  TEXT NOT NULL,                -- 'LONG' | 'SHORT'  (never GRAY)
  severity      REAL,                         -- e_loss, or commitment for forks
  notes_json    TEXT,                         -- kind-specific extras (e.g. peak_e for lost_advantage)
  created_at    TEXT NOT NULL
);

CREATE TABLE drill_attempts (
  id           INTEGER PRIMARY KEY,
  scenario_id  INTEGER NOT NULL REFERENCES scenarios(id),
  answer       TEXT NOT NULL,                 -- 'LONG' | 'SHORT'
  correct      INTEGER NOT NULL,
  response_ms  INTEGER,
  answered_at  TEXT NOT NULL
);
```

**Sample data:** `data/fixture.db` is committed. It is this schema populated by
`python -m pipeline fixture` (§4 A6) from five of the author's games at depth 12, so the
app can be tried without an engine.

---

## 4. Data pipeline  (`pipeline/`)

**Goal:** turn Chess.com archives into a fully populated `tutor.db`.

Everything runs as a CLI: `python -m pipeline <command>`. Idempotent — re-running skips
work already done (games by `url`, analysis by `(fen_key, depth)`).

### A1. Ingest — `python -m pipeline ingest [--since YYYY-MM] [--months N]`
- Fetch `https://api.chess.com/pub/player/<CHESSCOM_USER>/games/archives`, then each monthly URL.
  Send header `User-Agent: chessefforttutor (github.com/GrahamDerry/chessefforttutor)` — required or Chess.com blocks.
- Keep `time_class == "blitz"` only. Skip games with no `%clk` data (flag count).
- Parse PGN with `chess.pgn`. Populate `games` and `positions` (all columns except analysis fields).
- `user_color`, `result_user`, clocks per §2. Watch: clocks have tenths (`0:04:57.6`); the
  first move of each side compares against `base_seconds`.
- `phase`: opening until ply 24 or first non-book move (v1: just ply ≤ 24); endgame when
  no queens or total non-pawn material ≤ 13 (rook+bishop+knight-ish); else middlegame.

### A2. Engine wrapper — `pipeline/engine.py`
- One `chess.engine.SimpleEngine` per process; `MultiPV=4`; `Threads` = cores−1; `Hash` 512.
- `analyze(board) -> AnalysisResult` at `ANALYSIS_DEPTH`, plus a `SHALLOW_DEPTH` search.
- Cache through `analysis` table by `fen_key`. **Mate handling:** `E = 1.0` / `0.0`.
- Batch by game; commit per game so a crash loses ≤1 game.
- `python -m pipeline analyze [--limit N] [--depth D] [--workers N]` — fills `analysis`, then
  per position `e_best, e_played, e_loss, criticality, obvious, label`. Cache rows are
  committed as soon as they are searched, so the run is resumable and workers can share a DB.
- `--reshallow` refreshes only `shallow_best_move`/`obvious`/`label` after `SHALLOW_DEPTH` changes.

### A3. Commitment / fork pass — `python -m pipeline forks`
- Runs only on positions meeting the gate in §2. Uses depth 12 for the downstream searches.
- Writes `commitment`; upgrades `label` to LONG where `fork`.

### A4. Scenario generation — `python -m pipeline scenarios`
- Emits rows per §2 scenario table. Percentiles (`too_little`, `too_much`) computed per
  `time_control` across the user's own positions.
- `calm` sampling: match the LONG scenarios' distribution of `ply` (bucketed by 10) and
  `clock_before` (bucketed by 30 s) so the drill can't be solved by reading the clock.
  Target LONG:SHORT ≈ 50:50.
- Idempotent: clears and regenerates `scenarios` each run (drill_attempts reference
  scenario ids — keep ids stable by deterministic ordering, or store `position_id+kind`
  and re-link).

### A5. Report — `python -m pipeline report`
- Prints: game count, positions analyzed, criticality histogram (10 bins), label counts,
  scenario counts by kind, and per-phase mean `e_loss`. This is how we tune `config.py`.

### A6. Fixture — `python -m pipeline fixture`
- Runs A1–A4 on 5 games drawn at random from the last 6 months, at depth 12, and writes `data/fixture.db`. Random rather than most-recent so the drill is not dominated by games still fresh in memory.

### Tests (`tests/test_pipeline.py`)
- Clock arithmetic incl. increment and tenths; `E()` at 0, ±100, mate; criticality on a
  hand-built `candidates_json`; label rule truth table; `fen_key` normalization.

---

## 5. Web app  (`app/`)

**Goal:** the drill. Reads `data/tutor.db` (or whatever `TUTOR_DB` points at). Never
writes to any table except `drill_attempts`.

Run: `uvicorn app.main:app --reload` → http://localhost:8000

### B1. API (FastAPI, `app/main.py`, `app/db.py`)
```
GET  /api/drill/next?exclude=<ids>   → { scenario_id, fen, user_color, clock_before,
                                          base_seconds, increment, ply, history[] }
                                        # random scenario, balanced LONG/SHORT, prefer never-attempted;
                                        # at most one position per game per session (exclude = ids seen);
                                        # NEVER leaks kind, ground_truth, or result
POST /api/drill/answer               ← { scenario_id, answer: "LONG"|"SHORT", response_ms }
                                     → { correct, ground_truth, kind, criticality, obvious,
                                          commitment, move_played_san, seconds_spent,
                                          e_loss, best_move_san, pv_san[], game_url,
                                          result_user, explanation }   # see B4
GET  /api/stats                      → { attempts, accuracy, accuracy_by_kind{}, accuracy_by_phase{},
                                          recent[] }
GET  /api/scenarios/{id}             → full detail (for the review page)
```
- `explanation` in v1 is templated text from the numbers (e.g. "Only one move held the
  position (C = 0.31) and it was not the natural move — you spent 2.1 s and lost 0.24
  expected points."). LLM narration is a later swap-in behind the same field.

### B2. Drill page (`app/static/index.html`, `drill.js`)
- Board oriented to the user's color, side-to-move indicator, the **actual clock reading**
  shown as it was in the game. Nothing else — no opening name, no opponent, no result.
- The last few half-moves (`history`) are replayed on the board before the position is
  shown, one per second; any key skips the replay.
- Two big buttons: **Think long** / **Think short**. Keyboard: `L` / `S`.
- Measure `response_ms` from board render to click.
- On answer: reveal panel — correct/incorrect, the label and why, what you actually
  played and how long you took, engine's best line (SAN), link to the game on Chess.com.
  "Next" advances. Session counter in the corner.

### B3. Stats page (`stats.html`)
- Accuracy overall and by `kind` and `phase`; trend over the last 20 / 50 / 100 attempts.
  Simple tables or a small inline SVG — not a charting library.

### B4. Explanation templates (`app/explain.py`)
- One template per `kind` + a generic fallback. Pure function of the scenario row; unit-tested.

### Tests (`tests/test_app.py`)
- FastAPI `TestClient` against a temp DB built from `schema.sql`: next never returns
  GRAY, never leaks `ground_truth`; answer records an attempt and returns correct fields.

---

## 6. Repo conventions

```
README.md             how to run it
SPEC.md               this file
requirements.txt
pipeline/             data pipeline; schema.sql lives here
app/                  drill API + static frontend
data/                 fixture.db committed; tutor.db git-ignored
tests/
```

1. `app/` never imports from `pipeline/` except `pipeline/config.py` (for thresholds shown
   in explanations). `pipeline/` never imports from `app/`. The schema is the only shared
   surface.
2. Both sides read `TUTOR_DB` (default `data/tutor.db`), so either can be pointed at the
   fixture or at a scratch database.
3. Schema changes update `pipeline/schema.sql` and §3 of this file in the same commit.
4. Tests need neither Stockfish nor network access.

---

## 7. Out of scope (ideas for later)
Missed wins (opponent blundered, you didn't punish) · opening repertoire gap analysis ·
structural-distance fork detection · spaced repetition · LLM-written explanations ·
"find the move" mode · other time classes · hosting.
