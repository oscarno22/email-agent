FROM python:3.12-slim

# uv for fast, reproducible installs (pinned via uv.lock)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY agent/ ./agent/

WORKDIR /app/agent
# Install runtime deps only (no ruff/pytest); --frozen enforces uv.lock
RUN uv sync --no-dev --frozen

ENV PATH="/app/agent/.venv/bin:$PATH"

EXPOSE 2024
CMD ["uvicorn", "agent.ingestion.webapp:app", "--host", "0.0.0.0", "--port", "2024"]
