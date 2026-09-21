# Chess Effort Tutor

A local drill that trains one skill blitz players rarely practise: deciding **how long to
think**. It mines your own Chess.com blitz games, finds the positions where time was
misallocated, and asks a single question about each one:

> **Think long, or think short?**

Puzzle sites ask "what's the best move?", and by being a puzzle they already tell you the
position is critical. Real games never do. Here the board looks like any other moment from
one of your games, with the real clock reading. You answer LONG or SHORT, and only then see
whether the engine agrees, what you actually played, and how many seconds you spent.

## What a drill session looks like

1. The board is oriented to your colour. The last few half-moves are replayed so you have
   context (press any key to skip). Your clock is shown exactly as it was in the game.
   Nothing else is revealed: no opening name, no opponent, no result.
2. Answer with the two big buttons or the keyboard: `L` for **Think long**, `S` for
   **Think short**.
3. The reveal panel shows whether you were right and why, what you played and how long you
   took, the engine's best line, and a link to the game on Chess.com.
4. `Enter` or `Space` loads the next position. You get at most one position per game per
   session, so you can't learn a game rather than a skill.

Accuracy overall, by scenario kind, and by game phase is at `/stats`.

## Quickstart: try it in two minutes

The repository ships a small sample database built from five of the author's games, so you
can try the drill before installing an engine or fetching anything.

```bash
git clone https://github.com/GrahamDerry/chessefforttutor.git
cd chessefforttutor
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
TUTOR_DB=data/fixture.db .venv/bin/uvicorn app.main:app --reload
```

Open http://localhost:8000.

## Run it on your own games

You need [Stockfish](https://stockfishchess.org/) and a Chess.com account with blitz games
that have clock data (any game played on Chess.com in recent years does).

```bash
brew install stockfish                 # macOS. Windows: winget install Stockfish.Stockfish
export CHESSCOM_USER=your_chesscom_username
source .venv/bin/activate

python -m pipeline ingest --months 6   # download your blitz games -> data/tutor.db
python -m pipeline analyze --workers 3 # Stockfish at depth 18 on every position
python -m pipeline forks               # detect "quiet but committal" positions
python -m pipeline scenarios           # pick the positions worth drilling
python -m pipeline report              # sanity-check the distribution

uvicorn app.main:app --reload          # reads data/tutor.db by default
```

Some things worth knowing before you start `analyze`:

- **It is slow.** Depth 18 with four candidate lines on every position of six months of blitz
  (a few hundred games) takes hours on a laptop. `--workers N` runs N engine processes;
  use roughly one fewer than your core count.
- **It is resumable.** Every engine result is cached by position and committed immediately.
  Kill it whenever you like and re-run the same command; it skips finished games.
- **Every command is idempotent.** Re-running `ingest` only adds new games. `scenarios`
  regenerates the drill set but keeps the ids of anything you have already attempted.

For a quick look at your own data first, `python -m pipeline fixture` builds a five-game
sample at depth 12 into `data/fixture.db` in a few minutes.

## How it decides LONG or SHORT

All numbers are in **expected points** (0 to 1, the probability of winning plus half the
probability of drawing), never raw centipawns. Every threshold below lives in
`pipeline/config.py`. The formal definitions are in [SPEC.md](SPEC.md).

- **Criticality.** How much the decent-looking alternatives lose compared with the best move:
  the best move's expected points minus the mean of the next three. High criticality means
  only one move works.
- **Obviousness.** Does fast intuition already find the right move? Proxy: a depth-4 search
  picks the same move as the depth-18 search.
- **Commitment (forks).** Some quiet positions offer two near-equal moves that lead to very
  different kinds of game, and one of them is irreversible (a pawn move, a trade, castling).
  The pipeline follows each candidate's line and measures how sharp the resulting positions
  are. A large gap in sharpness means the choice deserves time even though nothing is
  immediately at stake.

The label is then:

| Label | When |
|---|---|
| **LONG** | critical and not obvious, or a fork |
| **SHORT** | calm and not a fork, or obvious and not a fork |
| GRAY | anything in between. Never shown in the drill. |

The label depends only on the position. What you *did* decides which **scenario kind** a
position is filed under, and the stats page breaks accuracy down by kind:

| Kind | Meaning |
|---|---|
| `blunder` | your move lost 0.20 or more expected points |
| `too_little` | LONG position, you lost material and spent less time than your own 25th percentile |
| `too_much` | SHORT position, you spent 10% or more of your remaining clock |
| `lost_advantage` | you were clearly winning and the game ended drawn or lost; the move where it slipped |
| `fork` | a commitment decision as described above |
| `calm` | SHORT positions sampled to match the LONG ones by move number and clock, so the drill can't be solved by reading the clock |

## Configuration

Everything is set through environment variables.

| Variable | Used by | Meaning |
|---|---|---|
| `CHESSCOM_USER` | `ingest`, `fixture` | Your Chess.com username. Required, no default. |
| `TUTOR_DB` | everything | Path to the SQLite database. Default `data/tutor.db`. |
| `STOCKFISH_PATH` | `analyze`, `forks`, `fixture` | Engine binary. Otherwise found on `PATH`, then Homebrew and winget locations. |
| `STOCKFISH_THREADS` | `analyze`, `forks` | Total engine threads. Default: cores minus one. |
| `STOCKFISH_HASH_MB` | `analyze`, `forks` | Hash table size. Default 512. Lower it on small machines. |

## Project layout

| Path | What |
|---|---|
| `pipeline/` | Chess.com ingest, Stockfish wrapper, scoring, fork detection, scenario generation, report. Run as `python -m pipeline <command>`. |
| `pipeline/config.py` | Every threshold, depth and constant. The place to tune. |
| `pipeline/schema.sql` | The SQLite schema. The only contract between pipeline and app. |
| `app/` | FastAPI drill API. Writes only the `drill_attempts` table. |
| `app/static/` | The frontend: plain HTML, CSS and JavaScript, no build step. |
| `data/` | `fixture.db` is committed as sample data. Your own `tutor.db` is git-ignored. |
| `tests/` | Pure-function and API tests. No engine or network needed. |
| `SPEC.md` | Design document: definitions, schema, component descriptions. |

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Limitations

- Blitz games only. Bullet, rapid and daily are ignored.
- Chess.com only. No Lichess import.
- Runs locally. No hosting, no accounts.
- Explanations are templated from the numbers, not written by a language model.
- The thresholds were tuned on one player rated around 2300 blitz. Weaker or stronger
  players may want to adjust `SHALLOW_DEPTH` and `CRIT` in `pipeline/config.py` after
  reading the output of `python -m pipeline report`.

## License

[MIT](LICENSE).
