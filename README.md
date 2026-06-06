# email-agent

A LangGraph agent that triages incoming Gmail messages — classifies each one
(newsletter, receipt, calendar invite, personal, work, junk) and decides on a
label / archive / draft action.

**Status:** personal-only v1, shadow mode. The graph runs end-to-end and logs the
action it *would* take. It does not yet read from Gmail or apply labels — those
come next (see [Roadmap](#roadmap)).

## Why

I wanted a LangGraph project that exercised more than checkpointing — multi-node
routing, conditional fan-out, eventual interrupts for risky actions — and was
useful enough I'd actually run it against my own inbox. The design is intended
to extend to multiple users later, but v1 is just for me.

## Architecture

```
START → extract_features → classify → [route by category] → action_<category> → END
```

- **`extract_features`** — cheap deterministic signals (unsubscribe markers,
  links, sender domain). No LLM.
- **`classify`** — forced tool use on Anthropic's API. Two-tier cost strategy:
  Haiku first; if confidence is below 0.6, escalate to Sonnet.
- **Conditional routing** — the classifier's category picks one of the action
  subgraphs.
- **Action nodes** — build an `ActionPlan` (labels, archive, optional draft).
  In shadow mode, plans are logged but not executed.

The classifier prompt is steered by a user-editable rules list in
[`rules.py`](src/agent/rules.py) — this is the chosen "learning" mechanism,
not adaptive fine-tuning. Edit those rules to nudge classification behavior.

State lives in [`state.py`](src/agent/state.py) as a Pydantic model and is
updated immutably between nodes. Every node appends to `state.log` for
traceability.

### Trust gradient

Actions are gated behind an explicit trust phase the user opts into:

1. **shadow** — log the plan, do nothing (current default)
2. **label** — apply Gmail labels
3. **archive** — label + archive out of inbox
4. **draft** — generate draft replies for human review

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

# 3. Or smoke-test against built-in fixtures
cd src/agent && uv run python -m agent.smoke
```

### Other commands

```bash
make format     # ruff format
make check      # ruff check --diff
make check-fix  # ruff check --fix
```

## Project layout

```
src/
  langgraph.json         # LangGraph CLI config (graph entry point, env file)
  .env                   # ANTHROPIC_API_KEY + optional LangSmith keys
  agent/
    state.py             # Pydantic schema — read this first
    graph.py             # Graph wiring (StateGraph + conditional edges)
    nodes.py             # extract_features, action_* nodes, trust-phase gate
    classifier.py        # Haiku → Sonnet escalation, forced tool use
    rules.py             # User-editable classification rules
    fixtures.py          # Sample emails for the smoke test
    smoke.py             # Runs the graph against all fixtures
    pyproject.toml       # uv project lives here, not at repo root
```

## Roadmap

In rough priority order:

1. **Gmail Push / Pub/Sub ingestion** — replace pasted JSON with real incoming
   mail. Gmail `users.watch` → GCP Pub/Sub → webhook → `users.history.list`.
2. **Graduate shadow → label phase** — wire `gmail.modify` so action stubs
   actually apply labels.
3. **Morning digest** — one summary email per day of what was processed.
4. **Classifier tuning** — add edge-case fixtures, iterate `rules.py` and the
   system prompt, watch for over-escalation to Sonnet.

## License

Personal project. No license file yet.
