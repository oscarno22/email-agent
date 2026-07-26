# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Gmail-triage agent that classifies each email (newsletter / receipt / calendar / personal / work / banking / application / assessment / junk) and decides on a label/archive/draft action. Personal-only v1, **live in label phase** — real emails come in via Gmail Push → Pub/Sub → webhook, get classified and labeled; nothing is archived and no reply drafts are written until the trust phase is raised (`archive`/`draft` add those on top).

The classification pipeline is a `langgraph` `StateGraph` (the open-source DSL), but it runs **in-process** inside a plain FastAPI app (`graph.ainvoke`) — there is no LangGraph Platform server, Postgres, or Redis. The webhook enqueues onto an in-process asyncio queue and a background worker drains it.

## Commands

All targets `cd` into `src/agent` and activate its `uv` venv. Run from the repo root:

```bash
make start       # uv sync + uvicorn --reload (FastAPI app on :2024, dashboard at http://localhost:2024/)
make format      # ruff format
make check       # ruff check --diff
make check-fix   # ruff check --fix
```

Other commands:

```bash
make dashboard   # standalone stats UI on :8765 (historical data only — no live SSE)
make backfill    # JSONL action logs → SQLite (idempotent)
make renew-watch # renew 7-day Gmail watch (operationally critical — expires silently)
make refresh-token # regenerate the Gmail OAuth refresh token (browser flow)
make digest      # run daily digest once manually
make quick-digest # run quick digest once manually (live Gmail list of new inbox mail)
make smoke       # run graph against fixture emails (requires ANTHROPIC_API_KEY or GEMINI_API_KEY)
```

Crons are no longer registered with a separate command — they run in-process via APScheduler when `ENABLE_CRONS=true` (see below).

Tests live in `src/agent/tests/` (run with `uv run pytest` from `src/agent/`). Coverage is partial — classifier + node-graph plumbing only.

## Layout & paths that trip people up

- The `uv` project root is **`src/agent/`** (its own `pyproject.toml`, `.venv`, `uv.lock`). Run `uv` commands from there, not the repo root.
- `make start` runs `uvicorn agent.ingestion.webapp:app` from `src/agent`.
- `.env` lives at **`src/.env`** (loaded via `load_dotenv()` in `webapp.py`), not the repo root. See `src/.env.example` for all required vars. At minimum: `ANTHROPIC_API_KEY` (or `GEMINI_API_KEY` — see LLM section), `PUBSUB_VERIFICATION_TOKEN`, `TRUST_PHASE`, and `GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN`. The app raises at startup if no Gmail credentials are found. `src/token.json` and `src/credentials.json` are deleted — env vars are now required everywhere.
- **`DIGEST_TO_EMAIL` gotcha**: the digest recipient is resolved as `os.getenv("DIGEST_TO_EMAIL") or "oscarnolen@gmail.com"` (note: `or`, **not** `getenv`'s default arg) in `core/nodes.py`, `crons/digest.py`, `crons/quick_digest.py`. The CloudFormation `DigestToEmail` param sets this env var; when it was empty the container ran with `DIGEST_TO_EMAIL=""`, which a `getenv(key, default)` does **not** override (the default only applies when the key is absent). That empty value silently broke *both* scheduled digest sends (`send_email(to="")` → HTTP 400) and the own-digest self-reply guard (`_is_own_digest`'s `sender == _DIGEST_TO` compared against `""`). The `or` form fixes both.
- `LANGSMITH_*` keys are optional tracing.
- Python is pinned to `>=3.12, <3.13`.

## Package layout

`src/agent/` is split into five subpackages:

| Subpackage | What it contains |
|---|---|
| `core/` | Graph pipeline (langgraph `StateGraph` DSL) — `state`, `graph`, `nodes`, `classifier`, `drafter`, `rules`, `llm` (provider abstraction + Anthropic/Gemini fallback) |
| `ingestion/` | Email delivery — `gmail_client` (OAuth + API wrapper), `webapp` (FastAPI app: webhook + worker queue + scheduler + dashboard routes), `batch_review` (bulk inbox processing) |
| `crons/` | Scheduled tasks — `digest` (daily summary), `quick_digest` (every-3h live Gmail list of new inbox mail; AI-independent), `digest_render` (shared MJML→HTML rendering for both digests), `renew_watch` (all expose `main()`, called by APScheduler and runnable manually) |
| `stats/` | Persistence & UI — `db` (SQLite), `events` (SSE bus), `backfill`, `dashboard` (APIRouter + standalone app) |
| `dev/` | Dev tooling — `fixtures` (sample emails), `smoke` (smoke test runner) |

## Architecture

Read **`src/agent/core/state.py`** first — it's the schema contract for everything.

The graph (`src/agent/core/graph.py`) is a linear pipeline with a fan-out at the end:

```
START → extract_features → classify → [route by category] → action_<category> → END
```

- **`extract_features`** (`core/nodes.py`): cheap deterministic signals (unsubscribe markers, links, sender-domain check). No LLM.
- **`classify`** (`core/classifier.py`): forced tool use on `classify_email`. **Two-tier cost strategy**: `Tier.FAST` first; if `confidence < 0.6`, escalate to `Tier.DEEP` and mark `needs_escalation=True`. Tiers map to concrete models inside `core/llm.py` (`FAST`→Haiku/Gemini Flash, `DEEP`→Sonnet/Gemini Flash). The category enum exposed to the model excludes `UNKNOWN` — `UNKNOWN` is reserved for code paths the model can't pick.
- **LLM provider abstraction** (`core/llm.py`): both `classify` and `generate_draft` go through `get_provider()`, which returns a `Provider` (`structured_completion` for forced-tool output, `text_completion` for prose). The factory composes providers from env keys: both `ANTHROPIC_API_KEY` + `GEMINI_API_KEY` present → `FallbackProvider(AnthropicProvider, GeminiProvider)` (Anthropic primary, Gemini auto-fallback on any `LLMError`); only one key → that provider alone; neither → raises `LLMError` at first use. Gemini uses `gemini-2.5-flash` for both tiers via the `google-genai` SDK; its JSON-schema response uses the same `CLASSIFY_TOOL` schema (numeric `minimum`/`maximum` keys are stripped — Gemini's response_schema is an OpenAPI-3 subset). The factory is `@lru_cache(maxsize=1)`; tests `patch("agent.core.classifier.get_provider")`.
- **AI-failure fallback**: classification fails before any Gmail mutation, so when **every** configured provider raises (surfaced as `LLMError`), the worker (`ingestion/webapp.py`) leaves the email untouched, files it under `Email Agent/Needs Review`, and emails a throttled alert (≤1/hour, `kv_store` key `last_ai_alert_ts`). With the Gemini fallback this only fires when Anthropic *and* Gemini both fail. Labeling + alert are best-effort and never crash the worker. Gmail-token failure is separate (logged loudly by `_gmail_call`).
- **Conditional routing**: `route_by_category` returns the category string; `CATEGORY_NODES` (a dict in `core/nodes.py`) maps each `Category` value to its action node. To add a category: add to the `Category` enum, add an entry to `CATEGORY_NODES`, write the action function. The graph wiring picks it up automatically.
- **Action nodes**: each builds an `ActionPlan` and calls `_action()`. **`_action()` enforces the trust gradient**: in `TrustPhase.SHADOW`, it rewrites the plan's notes to `[shadow] would: …` so nothing actually fires. Real Gmail mutations would gate on `trust_phase` here. After acting, `_append_action_log` records to SQLite via `record_event` and emits a live event over the in-process SSE bus via `events.publish()` — no JSONL file is written. (The old LangGraph `push_ui_message` generative-UI channel was removed with the server.)
- **Label hierarchy**: **category labels are top-level** (`_LABEL_PREFIX = ""` in `core/nodes.py`) — `Newsletters`, `Receipts`, `Calendar`, `Personal`, `Work`, `Banking`, `Applications`, `Applications/Assessments`, `Junk` — so they coexist with and *reuse* labels you already manage by hand instead of the agent creating a duplicate nested copy. Only the agent's own machinery stays under the `Email Agent/` parent: the digests (`Email Agent/Daily Digest`, `…/Quick Digest`) and operational labels (`Email Agent/Needs Review`, `…/Alerts`). `_get_label_id` (`ingestion/gmail_client.py`) reuses an existing label by **exact name** and otherwise creates it, building each missing ancestor segment first so a nested parent is a real, collapsible Gmail label rather than a synthetic name-only nesting. **Archive vs. label are independent**: archiving only removes the `INBOX` label (`apply_action`), so an archived email is still fully accessible under its category label; nothing is ever deleted. Only newsletter and junk archive; every other category (receipt/calendar/personal/work/banking/application/assessment) stays in the inbox.

The classifier prompt receives **user-editable rules** from `core/rules.py` (`DEFAULT_RULES`) prepended to each message. This is the chosen "learning" mechanism — *not* adaptive fine-tuning. Tweak `rules.py` to change classification behavior before touching the prompt.

State is a Pydantic `BaseModel` updated immutably via `model_copy(update=…)` between nodes (not LangGraph's typed-dict pattern). Each node appends to `state.log` for traceability.

## Dashboard

The stats dashboard is served by the FastAPI app in `webapp.py` — available at **`http://localhost:2024/`** when `make start` is running. Because it shares a process with the ingestion webhook and the in-process graph worker, the SSE live feed works in real time.

`stats/dashboard.py` exposes routes via `router = APIRouter()`, which `webapp.py` includes. A standalone `app` at the bottom of `dashboard.py` lets `make dashboard` still work on `:8765` for historical-data browsing without `make start`.

**Auth:** the dashboard is mounted into the *public* webapp, so the router is gated by `require_dashboard_auth` (`ingestion/auth.py`) — HTTP Basic Auth via `DASHBOARD_USER` (default `admin`) / `DASHBOARD_PASSWORD`. It is **fail-open when `DASHBOARD_PASSWORD` is unset** (local `make start`/`make dashboard`) and enforced whenever it's set (the secret is supplied in any deployment). `/health` and `/webhook/pubsub` stay open — Pub/Sub can't send Basic Auth, so the webhook keeps its `?token=` check. The mutating `POST /api/batch-review` route additionally **clamps** its requested `trust_phase` to the process `TRUST_PHASE` ceiling so a request can't escalate beyond the deployment's configured action level.

## Persistence

- **Stats**: SQLite (`src/stats.db`, gitignored). Tables `email_events`, `user_rules`, `kv_store`. Path overridable via `STATS_DB_PATH`. Schema is intentionally Postgres-portable — migration is a connection-string swap when needed.
- **Gmail history cursor**: stored in the SQLite `kv_store` table under key `"last_history_id"` via `get_cursor()` / `set_cursor()` in `stats/db.py`. In production this DB lives on EFS (`STATS_DB_PATH=/data/stats.db`), so the cursor survives redeploys.
- **Digest**: reads from SQLite via `get_events_for_date(date)` in `stats/db.py` — no dependency on JSONL files.

## Digest & draft email format

- **Digests are HTML.** Both `crons/digest.py` and `crons/quick_digest.py` send a `multipart/alternative` email: the existing pipe-delimited plaintext as the fallback, plus an HTML alternative built by **`crons/digest_render.py`**. The HTML is authored in [MJML](https://mjml.io/) and compiled **in-process** via the `mjml-python` package (Rust `mrml` bindings — no Node toolchain). Layout is a **mobile-first single-column stack of per-email cards** (`_card`/`_cards` in `digest_render.py`): each email is a full-width block (category badge + time on one row, then sender / bold subject / action stacked) with `word-break:break-word`, so it reflows on phones with no horizontal scroll. The old multi-column `<mj-table>` was removed because `mj-table` is a passthrough that doesn't stack on mobile. Color-coded category badges + summary count chips remain. All sender/subject/notes values are `html.escape`d before injection (they come from arbitrary email). HTML rendering is **best-effort**: a render exception is logged and the digest still sends plaintext-only, so a template bug can never block delivery.
- **`send_email(to, subject, body, html=None)`** (`ingestion/gmail_client.py`): when `html` is given it builds `multipart/alternative` (plaintext part first as fallback, HTML part second as preferred); otherwise it sends a plain `MIMEText`. `create_draft` is unchanged — reply drafts stay plaintext.
- **Draft replies** (`core/drafter.py`): the prompt instructs the model to open with `Dear <name>,` (name inferred from the sender, else `Dear there,`) and close with `Best,` / `Oscar Nolen`, with tone matched to the sender's register. To change this voice, edit `SYSTEM_PROMPT` there.
- **JSONL logs** (`src/logs/`): no longer written. `stats/backfill.py` is a one-off migration tool for importing historical JSONL into SQLite.

## Startup behaviour

`webapp.py` lifespan (in order): `check_credentials()` → `init_db()` → `attach_loop()` → start the background `_worker()` task. If `ENABLE_CRONS=true`, it also renews the Gmail watch once on startup and starts an `AsyncIOScheduler` (watch renewal every ~6 days, daily digest at 07:00 UTC, quick digest every 3h during waking hours — 7/10/13/16/19/22 America/New_York). The quick digest (`crons/quick_digest.py`) reads **live from Gmail** (`search_messages` + `fetch_email`), not from SQLite, so it lists new inbox mail even when classification failed; it tracks a cursor in `kv_store` (`last_quick_digest_ts`) to only show mail since the last run. Both digests label **and archive** their own sent email (`Email Agent/Daily Digest`, `Email Agent/Quick Digest`) so it stays out of the inbox but browsable under its label; because the send-time archive can race Gmail's self-delivery adding `INBOX` back, the worker (`_worker` in `webapp.py`) also detects own digests on arrival (`is_own_digest` from `core/nodes.py`), archives them, and skips classifying them (no LLM call, no stats event). If no Gmail credentials are found (neither env vars nor `token.json`), the app raises immediately rather than accepting requests that will fail. On shutdown the lifespan drains the queue (`_queue.join()`), cancels the worker, and shuts the scheduler down.

**Crons gating:** `ENABLE_CRONS` defaults to `false` so local dev never registers/renews the production Gmail watch or sends digests. Production sets it `true` (the CloudFormation `EnableCrons` parameter).

The `/health` endpoint (`GET /health`) returns `{"status": "ok"}` — used for health checks.

## Dev vs. production (single Gmail watch)

Gmail allows **one active push watch per account**, and dev + prod share one Pub/Sub topic and one ngrok static domain. So there is no parallel live pipeline:

- **Production owns the only live watch** — `ENABLE_CRONS=true`, the prod ngrok domain, and the prod Pub/Sub push subscription. Crons renew the watch on startup + every ~6 days, keeping it alive indefinitely as long as the task runs.
- **Local dev keeps `ENABLE_CRONS=false`** (the `.env.example` default) so a dev process never registers a watch or advances the shared history cursor. Exercise the graph with `make smoke` (fixtures) or the authenticated `POST /api/batch-review` against the live inbox in `shadow`/`label`.
- **To get live push locally** (rare): temporarily re-point the Pub/Sub push subscription at your local ngrok URL and `make renew-watch` locally — then re-point to prod when done. Forgetting to re-point leaves prod blind. A true second pipeline requires a separate Google account + topic + ngrok domain (out of scope for v1).

## Renewing access (when push stops)

Two things expire:

- **Gmail watch (7 days):** renewed automatically by crons in production. Run `make renew-watch` to renew manually.
- **OAuth refresh token:** does *not* normally expire — **unless the OAuth consent screen is in "Testing" mode, in which case Google revokes refresh tokens after 7 days.** Crons cannot fix this. Publish the consent screen to **"In production"** (Google Cloud Console → APIs & Services → OAuth consent screen → Publish app) to make tokens durable. To mint a new token, run `make refresh-token` (browser flow; needs `GMAIL_CLIENT_ID/SECRET` in `src/.env`), then paste the value into `src/.env` and `cloudformation/secrets.json` and run `make secrets-create`.

## AWS Deployment

Infrastructure lives in `cloudformation/`. All deployment targets are in the Makefile.

**First-time order (run once):**
```bash
make deploy-bootstrap  # creates GitHub Actions IAM role (OIDC — no long-lived keys)
# fill in cloudformation/secrets.json, then:
make secrets-create    # creates Secrets Manager secret
make build && make push  # MUST happen before deploy-infra (avoids CloudFormation hang)
make deploy-infra      # deploys CloudFormation stack (ECS, EFS, IAM, SGs)
```

**After that:** every push to `main` triggers GitHub Actions → `docker build` (`src/Dockerfile`) → ECR push → ECS task definition update.

**Image tags — `:latest` must always exist.** CI pushes **both** `:<git-sha>` (what ECS actually deploys) and `:latest`. The two paths disagree about which tag they reference, and only the dual push keeps them compatible: GitHub Actions renders the task definition with the immutable `:<git-sha>`, but the CloudFormation `EcrImageUri` parameter defaults to `:latest`, so **`make deploy-infra` rewrites the task definition to `:latest`**. When only SHA tags were pushed, ECR's "keep last 10" lifecycle policy eventually expired the bootstrap `:latest` manifest, and the next `make deploy-infra` pointed the task at a nonexistent image → `CannotPullContainerError`, service to 0 running tasks (happened 2026-07-26). Recovery without a rebuild: re-tag the current commit's image via `aws ecr batch-get-image … | aws ecr put-image --image-tag latest`.

**Key infra decisions:**
- No ALB — ngrok agent runs as a sidecar container, keeps the existing static domain (`mobilize-shrunk-endless.ngrok-free.dev`). No custom domain or ACM cert needed.
- The task runs **two containers**: the app (uvicorn on `:2024`, from `src/Dockerfile`) and ngrok. No Postgres/Redis — the graph runs in-process and the cursor lives in SQLite, so the licensed LangGraph server is gone.
- EFS mounts at `/data` hold the SQLite DB — both stats and the Gmail history cursor (`STATS_DB_PATH=/data/stats.db`).
- ECR repo is created by `make deploy-infra` as a pre-step (not in CloudFormation) to avoid early-validation circular dependency.
- Crons run in-process via APScheduler; production sets `EnableCrons=true` in the CloudFormation parameters.

**Updating secrets:**
```bash
# edit cloudformation/secrets.json, then:
make secrets-create
# force re-deploy to pick up new values:
aws ecs update-service --cluster email-agent --service email-agent --force-new-deployment --region us-east-1
```

### Async/blocking gotchas

- **Use the `@asynccontextmanager` lifespan** passed to `FastAPI(lifespan=...)` — not `@app.on_event(...)`. The lifespan owns the worker task and scheduler.
- **Wrap blocking I/O in `asyncio.to_thread(...)`** in async (request/lifespan) context — SQLite and Gmail-API calls block the event loop otherwise. Graph *nodes* are sync callables and are safe: LangGraph runs them in a thread-pool executor under `graph.ainvoke`, so their blocking Gmail/SQLite calls don't block the loop and need no rewrite.
- **`events.publish()` is called from graph nodes** (running in the thread pool) and hops back to the event loop via `call_soon_threadsafe`; it's a no-op if no loop is attached (smoke script, standalone `make dashboard`).
