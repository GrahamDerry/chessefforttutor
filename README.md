# Chess Effort Tutor

Drill one decision on positions from your own blitz games: **think long, or think short?**
See [SPEC.md](SPEC.md) for the full design.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install stockfish          # Bucket A only
```

## Run the drill app (Bucket B)

```bash
python tools/make_fake_db.py data/fake.db      # placeholder data until the pipeline lands
TUTOR_DB=data/fake.db uvicorn app.main:app --reload
```

Open http://localhost:8000. Press `L` / `S` to answer, `Enter` for the next position.
Stats are at `/stats`.

Once Bucket A commits `data/fixture.db` (or produces `data/tutor.db`), point at it instead:

```bash
TUTOR_DB=data/fixture.db uvicorn app.main:app --reload
```

`tools/make_fake_db.py` exists only to unblock frontend work and should be deleted
once real data is flowing.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Layout

| Path | Owner | What |
|---|---|---|
| `pipeline/` | Bucket A | Chess.com ingest, Stockfish analysis, scenario generation |
| `pipeline/schema.sql` | shared | Canonical DDL. Changes need a joint PR. |
| `app/` | Bucket B | FastAPI drill API + static frontend |
| `tools/` | Bucket B | Throwaway fake-data generator |
| `data/` | — | `fixture.db` committed; `tutor.db` and `fake.db` git-ignored |
