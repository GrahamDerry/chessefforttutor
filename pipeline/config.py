"""Every threshold, depth, path and constant the pipeline uses (SPEC.md §2, §4).

Nothing numeric is hardcoded anywhere else in pipeline/. Bucket B may import this
module for the thresholds it shows in explanations, so keep it dependency-light.
"""
from __future__ import annotations

import os
import shutil
from glob import glob
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "pipeline" / "schema.sql"
DEFAULT_DB = ROOT / "data" / "tutor.db"
FIXTURE_DB = ROOT / "data" / "fixture.db"


def db_path() -> Path:
    """Both buckets read TUTOR_DB (SPEC.md §6 rule 4)."""
    return Path(os.environ.get("TUTOR_DB", DEFAULT_DB))


# --------------------------------------------------------------------------- player / API
USERNAME = "Reitsy"                       # display name; matched case-insensitively
USER_AGENT = "chessefforttutor (github.com/GrahamDerry/chessefforttutor)"
API_BASE = "https://api.chess.com/pub/player"
TIME_CLASS = "blitz"                      # v1: blitz only
RULES = "chess"                           # skip chess960 and other variants
DEFAULT_MONTHS = 6                        # `ingest` with no flags = last 6 months (M2)
HTTP_TIMEOUT_S = 60
HTTP_RETRIES = 4

# --------------------------------------------------------------------------- engine
ANALYSIS_DEPTH = 18
SHALLOW_DEPTH = 4                         # "fast intuition" proxy; may go up for a 2300 player
FIXTURE_DEPTH = 12
FORK_DEPTH = 12                           # downstream searches in the commitment pass
MULTIPV = 4
OPPONENT_MULTIPV = 1                      # opponent plies only feed e_played (next-ply E); one line is enough
MAX_SEARCH_SECONDS = 20.0                 # cap per deep search: "depth D or this many seconds, whichever first"
WORKERS = 1                               # engine processes; each gets THREADS // WORKERS threads
HASH_MB = int(os.environ.get("STOCKFISH_HASH_MB", 512))   # env override for low-RAM machines
SHALLOW_HASH_MB = 16                      # tiny separate instance, fresh TT per probe
THREADS = int(os.environ.get("STOCKFISH_THREADS", max(1, (os.cpu_count() or 2) - 1)))
SHALLOW_THREADS = 1                       # deterministic shallow probes
PV_MAX_PLIES = 16                         # cap stored PV length per candidate
STOCKFISH_FALLBACKS = [
    "/opt/homebrew/bin/stockfish",
    "/usr/local/bin/stockfish",
]


def stockfish_path() -> str:
    """STOCKFISH_PATH env, then PATH, then known install locations."""
    env = os.environ.get("STOCKFISH_PATH")
    if env:
        return env
    found = shutil.which("stockfish")
    if found:
        return found
    for p in STOCKFISH_FALLBACKS:
        if Path(p).exists():
            return p
    local = os.environ.get("LOCALAPPDATA")
    if local:  # winget on Windows
        hits = sorted(glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                        "Stockfish.Stockfish*", "**", "stockfish*.exe"),
                           recursive=True))
        if hits:
            return hits[-1]
    raise FileNotFoundError(
        "Stockfish not found. Install it (brew install stockfish / winget install "
        "Stockfish.Stockfish) or set STOCKFISH_PATH.")


# --------------------------------------------------------------------------- expected points
E_SCALE = 0.00368                         # E(cp) = 1 / (1 + exp(-E_SCALE * cp))

# --------------------------------------------------------------------------- labels (§2)
CRIT = 0.10                               # LONG if C >= CRIT and not obvious
CALM = 0.03                               # SHORT if C <= CALM (and no fork)
FORK = 0.08                               # fork if commitment >= FORK
NEAR_EQUAL_E = 0.03                       # candidates within this E of best are "near-equal"
FORK_PV_PLIES = 6                         # follow each candidate's PV this far

# --------------------------------------------------------------------------- phase (§4 A1)
OPENING_MAX_PLY = 24
ENDGAME_MATERIAL = 13                     # total non-pawn, non-king material of BOTH sides
PIECE_VALUES = {chess.QUEEN: 9, chess.ROOK: 5, chess.BISHOP: 3, chess.KNIGHT: 3}

# --------------------------------------------------------------------------- scenarios (§2)
BLUNDER_E_LOSS = 0.20
TOO_LITTLE_E_LOSS = 0.10
TOO_LITTLE_PERCENTILE = 25                # of the user's seconds_spent, per time control
TOO_MUCH_TIME_FRACTION = 0.10
LOST_ADVANTAGE_PEAK_E = 0.75
LOST_ADVANTAGE_MAX_RESULT = 0.5
CALM_PLY_BUCKET = 10
CALM_CLOCK_BUCKET_S = 30.0
CALM_MIN = 10                             # floor so small DBs (the fixture) still get calm rows
CALM_SEED = 20260919                      # deterministic sampling -> stable scenario ids

# --------------------------------------------------------------------------- fixture / report
FIXTURE_GAMES = 5
# 10 criticality bins for the report. Non-uniform so CALM and CRIT are bin edges and the
# low range, where almost all positions live, is actually visible.
CRIT_HIST_EDGES = [0.0, 0.01, 0.02, CALM, 0.05, 0.075, CRIT, 0.15, 0.20, 0.30, 1.0]
