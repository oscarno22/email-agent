NGROK_DOMAIN ?= mobilize-shrunk-endless.ngrok-free.dev
LANGGRAPH_PORT ?= 2024

.PHONY: start start-docker ngrok format check check-fix renew-watch digest setup-crons

help:
	@echo "Available commands:"
	@echo "  start        - Start agent server in local dev (langgraph dev)"
	@echo "  start-docker - Start agent server in Docker (langgraph up, requires license)"
	@echo "  ngrok        - Start ngrok tunnel (NGROK_DOMAIN=$(NGROK_DOMAIN))"
	@echo "  setup-crons  - Register watch renewal + digest crons on the running server"
	@echo "  renew-watch  - Run watch renewal once manually"
	@echo "  digest       - Run digest once manually"
	@echo "  format       - Format codebase"
	@echo "  check        - Run linters and type checks"
	@echo "  check-fix    - Run linters and type checks with auto-fix"

start:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	cd .. && \
	langgraph dev

ngrok:
	ngrok http --url=$(NGROK_DOMAIN) $(LANGGRAPH_PORT)

start-docker:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	cd .. && \
	langgraph up

renew-watch:
	cd src/agent && \
	source .venv/bin/activate && \
	uv run python -m agent.renew_watch

digest:
	cd src/agent && \
	source .venv/bin/activate && \
	uv run python -m agent.digest

setup-crons:
	cd src/agent && \
	source .venv/bin/activate && \
	uv run python -m agent.setup_crons

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
