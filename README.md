# email-agent

A LangGraph agent that triages incoming Gmail messages — classifies each one
(newsletter, receipt, calendar invite, personal, work, junk) and decides on a
label / archive / draft action.

**Status:** personal-only v1, **live in draft phase**. Real emails come in via
Gmail Push → Pub/Sub → webhook, get classified, labeled, and (for personal/work)
have a reply draft generated. Stats are persisted to SQLite and visible on a
real-time dashboard at `http://localhost:2024/` (served by the same process as
the webhook, so the SSE live feed works).

## Why

I wanted a LangGraph project that exercised more than checkpointing — multi-node
routing, conditional fan-out, trust-gated actions — and was useful enough I'd
actually run it against my own inbox. The design is intended to extend to
multiple users later, but v1 is just for me.

## Architecture

```
Gmail Push → Pub/Sub → POST /webhook/pubsub
                          │
                          ▼
START → extract_features → classify → [route by category] → action_<category> → END
                                                                  │
                                                                  ├─► Gmail API (label / archive / draft)
                                                                  ├─► JSONL log + SQLite stats
                                                                  └─► push_ui_message → SSE → dashboard
```

- **`extract_features`** — cheap deterministic signals (unsubscribe markers,
  links, sender domain). No LLM.
- **`classify`** — forced tool use on Anthropic's API. Two-tier cost strategy:
  Haiku first; if confidence is below 0.6, escalate to Sonnet.
- **Conditional routing** — the classifier's category picks one of the action
  subgraphs.
- **Action nodes** — build an `ActionPlan` (labels, archive, optional draft).
  Trust-phase-gated; see below.
- **Stats sink** — every processed email is appended to a daily JSONL log *and*
  inserted into `src/stats.db` (SQLite). The dashboard reads from SQLite.
- **Generative UI** — action nodes call `push_ui_message("email_card", …)` so
  cards flow through the LangGraph stream / state, and the same payload is
  fanned out over SSE to the dashboard's live feed.
- **Batch review** — `POST /api/batch-review` fetches all unread inbox messages
  and runs each through the triage agent. Progress streams over SSE; the
  dashboard has a control panel with trust-phase selector, mark-read toggle,
  and max-emails input.

The classifier prompt is steered by a user-editable rules list in
[`core/rules.py`](src/agent/core/rules.py) — this is the chosen "learning"
mechanism, not adaptive fine-tuning. Edit those rules to nudge classification
behavior.

State lives in [`core/state.py`](src/agent/core/state.py) as a Pydantic model
and is updated immutably between nodes. Every node appends to `state.log` for
traceability.

### Trust gradient

Actions are gated behind an explicit trust phase the user opts into. Set via
the `TRUST_PHASE` env var:

1. **shadow** — log the plan, do nothing
2. **label** — apply Gmail labels
3. **archive** — label + archive out of inbox *(folded into `label`/`draft` plans)*
4. **draft** — generate draft replies for human review *(current default in `webapp.py`)*

Higher phases are not enabled by default and never will be — graduating happens
per-user, by choice.

## Quickstart

Requires Python 3.12 and [`uv`](https://github.com/astral-sh/uv).

```bash
# 1. Set up env
cp src/.env.example src/.env
# ...then fill in ANTHROPIC_API_KEY (LANGSMITH_* keys optional for tracing)

# 2. Run the graph in LangGraph Studio
make start
# Studio opens; paste an email JSON into the input to invoke the graph.
# Dashboard with live SSE feed is at http://localhost:2024/

# 3. Smoke-test against built-in fixtures
cd src/agent && uv run python -m agent.dev.smoke
```

For the live ingestion pipeline (Pub/Sub → webhook → graph) see
[Dev setup](#dev-setup-live-ingestion).

### Other commands

```bash
make backfill      # JSONL action logs → SQLite (idempotent)
make renew-watch   # renew the 7-day Gmail watch
make digest        # run morning digest once
make setup-crons   # register watch-renewal + digest crons on the LangGraph server
make format        # ruff format
make check         # ruff check --diff
make check-fix     # ruff check --fix
```

### Processes at a glance

The full application is two concurrent processes:

| Process | Command | What it does |
|---|---|---|
| LangGraph server | `make start` | Runs the graph, webhook, and dashboard on `:2024`. The only required process. |
| ngrok tunnel | `make ngrok` | Forwards GCP Pub/Sub push notifications to `localhost:2024`. Required for live ingestion; not needed when testing with fixtures. |

`make dashboard` starts a standalone stats UI on `:8765` for browsing historical data without `make start` — but the SSE live feed is inactive in that mode (separate process).

Scheduled tasks (`digest`, `renew_watch`) run inside the LangGraph server as registered crons — not as separate processes. They're registered once with `make setup-crons`.

### Dev setup (live ingestion)

1. **Terminal 1:** `make start` — LangGraph server + dashboard on `:2024`
2. **Terminal 2:** `ngrok http --url=<your-domain> 2024` (or `make ngrok`)
3. `src/.env` needs at minimum: `ANTHROPIC_API_KEY`, `PUBSUB_VERIFICATION_TOKEN`, `TRUST_PHASE=draft`
4. `src/credentials.json` + `src/token.json` (Gmail OAuth, gitignored)
5. Gmail watch must be active — `make renew-watch` if it's stale (expires every 7 days)
6. Open `http://localhost:2024/` for the live dashboard

## Project layout

```
src/
  langgraph.json         # LangGraph CLI config (graphs + http app + env)
  .env                   # ANTHROPIC_API_KEY, TRUST_PHASE, PUBSUB_VERIFICATION_TOKEN, ENABLE_CRONS
  stats.db               # SQLite stats DB (gitignored)
  # Gmail history cursor stored in LangGraph store (InMemoryStore in dev, Postgres in prod)
  logs/YYYY-MM-DD.jsonl  # daily append-only action logs (digest input)
  agent/
    pyproject.toml       # uv project root

    core/                # LangGraph graph pipeline
      state.py           # Pydantic schema (incl. UI message reducer) — read this first
      graph.py           # StateGraph wiring + conditional edges
      nodes.py           # extract_features, action_*, trust-phase gate, stats + UI emit
      classifier.py      # Haiku → Sonnet escalation, forced tool use
      drafter.py         # Reply draft generation for personal/work emails
      rules.py           # User-editable classification rules

    ingestion/           # Email delivery: Gmail push → webhook → graph run
      gmail_client.py    # OAuth2 + Gmail API (history, fetch, modify, watch, drafts, send)
      webapp.py          # POST /webhook/pubsub + dashboard routes (merged for in-process SSE)
      batch_review.py    # Bulk inbox review — fetches unread messages and runs each through the agent

    crons/               # Scheduled tasks (run inside the LangGraph server)
      digest.py          # Daily summary email from yesterday's JSONL
      digest_graph.py    # LangGraph graph wrapper for the digest cron
      renew_watch.py     # Watch-renewal script (also runnable standalone)
      renew_watch_graph.py # LangGraph graph wrapper for the watch-renewal cron
      setup_crons.py     # Registers crons via the LangGraph SDK (ENABLE_CRONS gated)

    stats/               # Persistence and dashboard UI
      db.py              # SQLite stats sink (portable schema, Postgres-ready)
      backfill.py        # One-off JSONL → SQLite import
      events.py          # In-process pub/sub bus that powers the dashboard SSE
      dashboard.py       # APIRouter with all dashboard routes; standalone app for make dashboard

    dev/                 # Developer tooling (not used in production)
      fixtures.py        # Sample emails for smoke testing
      smoke.py           # Runs the graph against all fixtures
```

## Stats & dashboard

- **`db.py`** — single `email_events` table; portable SQL (`?` params, ISO-8601
  text timestamps) so the AWS migration to Postgres is roughly a connect-call
  swap plus `AUTOINCREMENT` → `SERIAL`. Override path with `STATS_DB_PATH`.
- **`dashboard.py`** — inline HTML + Chart.js from CDN. Routes exposed via
  `APIRouter` and included in `webapp.py`, so they run in the same process as
  the ingestion webhook. Endpoints:
  - `GET /` — page (at `http://localhost:2024/` when `make start` is running)
  - `GET /api/stats` — totals, category breakdown, daily counts, top senders
  - `GET /api/events?limit=N` — recent events
  - `GET /api/stream` — SSE feed of live `email_processed` and batch-progress events
  - `POST /api/batch-review` — start a bulk inbox run
  - `GET /api/batch-review/status` — poll batch run state
- **SSE live feed** works because `dashboard.py` and `webapp.py` share a process
  (same in-memory `events.py` bus). `make dashboard` (`:8765`) is a standalone
  fallback that only has historical data.

## Roadmap

In rough priority order:

1. **Classifier tuning** — iterate `rules.py` based on real-mail
   misclassifications; watch for over-escalation to Sonnet.
2. **AWS deployment** — ECS/Fargate, ALB replacing ngrok, Secrets Manager for
   Gmail credentials, swap SQLite → RDS Postgres (the schema is already portable).
3. **Approval surface for drafts** — Slack / web UI / Gmail drafts (Gmail drafts
   is the current default; revisit when multi-tenant).

## License

Personal project. No license file yet.
