# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A LangGraph-based agent that triages incoming Gmail messages — classifies each email (newsletter / receipt / calendar / personal / work / junk) and decides on a label/archive/draft action. Personal-only v1, **live in draft phase** — real emails come in via Gmail Push → Pub/Sub → webhook, get classified, labeled, and (for personal/work) have a reply draft generated.

## Commands

All targets `cd` into `src/agent` and activate its `uv` venv. Run from the repo root:

```bash
make start       # uv sync + langgraph dev (LangGraph server on :2024, dashboard at http://localhost:2024/)
make format      # ruff format
make check       # ruff check --diff
make check-fix   # ruff check --fix
```

Other commands:

```bash
make dashboard   # standalone stats UI on :8765 (historical data only — no live SSE)
make backfill    # JSONL action logs → SQLite (idempotent)
make renew-watch # renew 7-day Gmail watch (operationally critical — expires silently)
make digest      # run morning digest once manually
make setup-crons # register watch-renewal + digest crons on the running LangGraph server
make smoke       # run graph against fixture emails (requires ANTHROPIC_API_KEY)
```

No test suite yet — `pytest` is in dev deps but unused.

## Layout & paths that trip people up

- The `uv` project root is **`src/agent/`** (its own `pyproject.toml`, `.venv`, `uv.lock`). Run `uv` commands from there, not the repo root.
- `langgraph dev` must run from **`src/`** (where `langgraph.json` lives). `make start` handles this.
- `.env` lives at **`src/.env`** (loaded by `langgraph.json`), not the repo root. See `src/.env.example` for all required vars. At minimum: `ANTHROPIC_API_KEY`, `PUBSUB_VERIFICATION_TOKEN`, `TRUST_PHASE`, and either the `GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN` trio or a local `src/token.json`. The server raises at startup if no Gmail credentials are found.
- `LANGSMITH_*` keys are optional tracing.
- Python is pinned to `>=3.12, <3.13`.

## Package layout

`src/agent/` is split into five subpackages:

| Subpackage | What it contains |
|---|---|
| `core/` | LangGraph graph pipeline — `state`, `graph`, `nodes`, `classifier`, `drafter`, `rules` |
| `ingestion/` | Email delivery — `gmail_client` (OAuth + API wrapper), `webapp` (webhook + dashboard routes), `batch_review` (bulk inbox processing) |
| `crons/` | Scheduled tasks — `digest`, `renew_watch`, their LangGraph graph wrappers, `setup_crons` |
| `stats/` | Persistence & UI — `db` (SQLite), `events` (SSE bus), `backfill`, `dashboard` (APIRouter + standalone app) |
| `dev/` | Dev tooling — `fixtures` (sample emails), `smoke` (smoke test runner) |

## Architecture

Read **`src/agent/core/state.py`** first — it's the schema contract for everything.

The graph (`src/agent/core/graph.py`) is a linear pipeline with a fan-out at the end:

```
START → extract_features → classify → [route by category] → action_<category> → END
```

- **`extract_features`** (`core/nodes.py`): cheap deterministic signals (unsubscribe markers, links, sender-domain check). No LLM.
- **`classify`** (`core/classifier.py`): forced tool use on `classify_email`. **Two-tier cost strategy**: Haiku first; if `confidence < 0.6`, escalate to Sonnet and mark `needs_escalation=True`. The category enum exposed to the model excludes `UNKNOWN` — `UNKNOWN` is reserved for code paths the model can't pick.
- **Conditional routing**: `route_by_category` returns the category string; `CATEGORY_NODES` (a dict in `core/nodes.py`) maps each `Category` value to its action node. To add a category: add to the `Category` enum, add an entry to `CATEGORY_NODES`, write the action function. The graph wiring picks it up automatically.
- **Action nodes**: each builds an `ActionPlan` and calls `_action()`. **`_action()` enforces the trust gradient**: in `TrustPhase.SHADOW`, it rewrites the plan's notes to `[shadow] would: …` so nothing actually fires. Real Gmail mutations would gate on `trust_phase` here. After acting, `_append_action_log` records to SQLite via `record_event` and emits a UI event — no JSONL file is written.

The classifier prompt receives **user-editable rules** from `core/rules.py` (`DEFAULT_RULES`) prepended to each message. This is the chosen "learning" mechanism — *not* adaptive fine-tuning. Tweak `rules.py` to change classification behavior before touching the prompt.

State is a Pydantic `BaseModel` updated immutably via `model_copy(update=…)` between nodes (not LangGraph's typed-dict pattern). Each node appends to `state.log` for traceability.

## Dashboard

The stats dashboard is served by `webapp.py` (the LangGraph `http.app`) — available at **`http://localhost:2024/`** when `make start` is running. Because it shares a process with the ingestion webhook, the SSE live feed works in real time.

`stats/dashboard.py` exposes routes via `router = APIRouter()`, which `webapp.py` includes. A standalone `app` at the bottom of `dashboard.py` lets `make dashboard` still work on `:8765` for historical-data browsing without `make start`.

## Persistence

- **Stats**: SQLite (`src/stats.db`, gitignored). Single table `email_events`. Path overridable via `STATS_DB_PATH`. Schema is intentionally Postgres-portable — migration is a connection-string swap when needed.
- **Gmail history cursor**: stored in the LangGraph store under namespace `("webhook",)`, key `"last_history_id"`. Uses `InMemoryStore` in `langgraph dev`, Postgres-backed automatically in production. No `last_history_id.txt` file.
- **Digest**: reads from SQLite via `get_events_for_date(date)` in `stats/db.py` — no dependency on JSONL files.
- **JSONL logs** (`src/logs/`): no longer written. `stats/backfill.py` is a one-off migration tool for importing historical JSONL into SQLite.

## Startup behaviour

`webapp.py` lifespan (in order): `check_credentials()` → `init_db()` → `attach_loop()`. If no Gmail credentials are found (neither env vars nor `token.json`), the server raises immediately rather than accepting requests that will fail.

The `/health` endpoint (`GET /health`) returns `{"status": "ok"}` — used for load balancer health checks.

### LangGraph dev gotchas

- **`@app.on_event("startup")` is rejected** — LangGraph dev calls `validate_router_lifespan_hooks` and refuses `on_startup` handlers on the custom http.app. Use `@asynccontextmanager` lifespan passed to `FastAPI(lifespan=...)` instead.
- **Blocking I/O raises `BlockingError`** — LangGraph dev runs blockbuster to detect synchronous blocking calls in the async event loop. All SQLite and filesystem calls in async context must be wrapped in `asyncio.to_thread(...)`.
