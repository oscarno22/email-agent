.PHONY: start start-docker format check check-fix

help:
	@echo "Available commands:"
	@echo "  start        - Start agent server in local dev (langgraph dev)"
	@echo "  start-docker - Start agent server in Docker (langgraph up)"
	@echo "  format       - Format codebase"
	@echo "  check        - Run linters and type checks"
	@echo "  check-fix    - Run linters and type checks with auto-fix"

start:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	cd .. && \
	langgraph dev

start-docker:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	cd .. && \
	langgraph up

format:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	uv run ruff format .

check:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	uv run ruff check --diff .

check-fix:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	uv run ruff check --fix .
