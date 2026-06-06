# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A LangGraph-based agent that triages incoming Gmail messages — classifies each email (newsletter / receipt / calendar / personal / work / junk) and decides on a label/archive/draft action. Currently personal-only v1 in **shadow mode** (logs the action it *would* take, does not yet touch Gmail). Architected to be multi-tenant SaaS-ready later.

## Commands

All targets `cd` into `src/agent` and activate its `uv` venv. Run from the repo root:

```bash
make start       # uv sync + langgraph dev (LangGraph Studio UI for poking the graph)
make format      # ruff format
make check       # ruff check --diff
make check-fix   # ruff check --fix
```

Smoke test the graph against fixture emails (requires `ANTHROPIC_API_KEY`):

```bash
cd src/agent && uv run python -m agent.smoke
```

No test suite yet — `pytest` is in dev deps but unused.

## Layout & paths that trip people up

- The `uv` project root is **`src/agent/`** (its own `pyproject.toml`, `.venv`, `uv.lock`). Run `uv` commands from there, not the repo root.
- `langgraph dev` must run from **`src/`** (where `langgraph.json` lives). `make start` handles this.
- `.env` lives at **`src/.env`** (loaded by `langgraph.json`), not the repo root. `ANTHROPIC_API_KEY` is required; `LANGSMITH_*` keys are optional tracing.
- Python is pinned to `>=3.12, <3.13`.

## Architecture

Read **`src/agent/state.py`** first — it's the schema contract for everything.

The graph (`src/agent/graph.py`) is a linear pipeline with a fan-out at the end:

```
START → extract_features → classify → [route by category] → action_<category> → END
```

- **`extract_features`** (`nodes.py`): cheap deterministic signals (unsubscribe markers, links, sender-domain check). No LLM.
- **`classify`** (`classifier.py`): forced tool use on `classify_email`. **Two-tier cost strategy**: Haiku first; if `confidence < 0.6`, escalate to Sonnet and mark `needs_escalation=True`. The category enum exposed to the model excludes `UNKNOWN` — `UNKNOWN` is reserved for code paths the model can't pick.
- **Conditional routing**: `route_by_category` returns the category string; `CATEGORY_NODES` (a dict in `nodes.py`) maps each `Category` value to its action node. To add a category: add to the `Category` enum, add an entry to `CATEGORY_NODES`, write the action function. The graph wiring picks it up automatically.
- **Action nodes**: each builds an `ActionPlan` and calls `_action()`. **`_action()` enforces the trust gradient**: in `TrustPhase.SHADOW`, it rewrites the plan's notes to `[shadow] would: …` so nothing actually fires. Real Gmail mutations would gate on `trust_phase` here.

The classifier prompt receives **user-editable rules** from `rules.py` (`DEFAULT_RULES`) prepended to each message. This is the chosen "learning" mechanism — *not* adaptive fine-tuning. Tweak `rules.py` to change classification behavior before touching the prompt.

State is a Pydantic `BaseModel` updated immutably via `model_copy(update=…)` between nodes (not LangGraph's typed-dict pattern). Each node appends to `state.log` for traceability.

## What's not built yet

The big missing piece is the **trigger**: Gmail Push → GCP Pub/Sub → webhook → `users.history.list`. Today the graph runs against pasted JSON in LangGraph Studio or fixtures from `fixtures.py`. The action nodes also don't yet call `gmail.modify` — graduating from shadow → label phase requires wiring real Gmail API calls behind the trust-phase check in `_action()`.
