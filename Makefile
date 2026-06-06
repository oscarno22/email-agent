.PHONY: start

help:
	@echo "Available commands:"
	@echo "  start - Start agent server in local dev"

start:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	cd .. && \
	langgraph dev
