.PHONY: start start-docker format check check-fix renew-watch digest setup-cron

help:
	@echo "Available commands:"
	@echo "  start        - Start agent server in local dev (langgraph dev)"
	@echo "  start-docker - Start agent server in Docker (langgraph up, requires license)"
	@echo "  renew-watch  - Renew Gmail push watch (expires every 7 days)"
	@echo "  digest       - Send yesterday's email digest"
	@echo "  setup-cron   - Print crontab lines for watch renewal + digest"
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

renew-watch:
	cd src/agent && \
	source .venv/bin/activate && \
	uv run python -m agent.renew_watch

digest:
	cd src/agent && \
	source .venv/bin/activate && \
	uv run python -m agent.digest

setup-cron:
	@echo "Add these lines to your crontab ('crontab -e'):"
	@echo ""
	@echo "# Email agent — renew Gmail watch every 6 days at 9am"
	@echo "0 9 */6 * * cd $(CURDIR) && make renew-watch >> /tmp/email-agent-renew.log 2>&1"
	@echo ""
	@echo "# Email agent — morning digest at 7am daily"
	@echo "0 7 * * * cd $(CURDIR) && make digest >> /tmp/email-agent-digest.log 2>&1"

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
