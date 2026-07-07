NGROK_DOMAIN ?= mobilize-shrunk-endless.ngrok-free.dev
LANGGRAPH_PORT ?= 2024
DASHBOARD_PORT ?= 8765

AWS_REGION  ?= us-east-1
AWS_ACCOUNT ?= 176331939239
ECR_REGISTRY = $(AWS_ACCOUNT).dkr.ecr.$(AWS_REGION).amazonaws.com
ECR_REPO    = email-agent
STACK_NAME  = email-agent
SECRET_NAME = email-agent/production

# Production agent behaviour. CloudFormation reuses previous parameter values on
# update, so these must be passed explicitly on every deploy-infra or the stack
# silently keeps whatever it had. Override on the CLI, e.g. TRUST_PHASE=draft.
TRUST_PHASE  ?= label
ENABLE_CRONS ?= true

.PHONY: start ngrok format check check-fix test renew-watch refresh-token digest quick-digest dashboard backfill smoke \
        deploy-bootstrap secrets-create deploy-infra build push logs status

help:
	@echo "Local dev:"
	@echo "  start           - Start FastAPI app in local dev (uvicorn --reload on :$(LANGGRAPH_PORT))"
	@echo "  ngrok           - Start ngrok tunnel (NGROK_DOMAIN=$(NGROK_DOMAIN))"
	@echo "  dashboard       - Start standalone stats dashboard on http://localhost:$(DASHBOARD_PORT)"
	@echo "  backfill        - Backfill JSONL action logs into SQLite stats DB"
	@echo "  renew-watch     - Run watch renewal once manually"
	@echo "  refresh-token   - Regenerate the Gmail OAuth refresh token (browser flow)"
	@echo "  digest          - Run daily digest once manually"
	@echo "  quick-digest    - Run quick digest once manually (live Gmail list of new mail)"
	@echo "  smoke           - Run graph against fixture emails"
	@echo "  format          - Format codebase"
	@echo "  check           - Run linters"
	@echo "  check-fix       - Run linters with auto-fix"
	@echo ""
	@echo "AWS deployment (first-time order):"
	@echo "  1. deploy-bootstrap - One-time: create GitHub Actions OIDC provider + IAM role"
	@echo "  2. secrets-create   - Create/update Secrets Manager secret from cloudformation/secrets.json"
	@echo "  3. build + push     - Build and push image to ECR BEFORE deploy-infra (avoids hang)"
	@echo "  4. deploy-infra     - Deploy/update CloudFormation stack (infra + ECS service)"
	@echo "  After step 4, push to main and GitHub Actions handles all future deploys."
	@echo ""
	@echo "AWS operations:"
	@echo "  build   - Build Docker image locally (uv + uvicorn, requires Docker)"
	@echo "  push    - Push image to ECR"
	@echo "  logs    - Tail ECS container logs (Ctrl-C to stop)"
	@echo "  status  - Show ECS service task counts"

start:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	set -a && source ../.env && set +a && \
	uv run uvicorn agent.ingestion.webapp:app --host 0.0.0.0 --port $(LANGGRAPH_PORT) --reload

ngrok:
	ngrok http --url=$(NGROK_DOMAIN) $(LANGGRAPH_PORT)

renew-watch:
	cd src/agent && \
	source .venv/bin/activate && \
	set -a && source ../.env && set +a && \
	uv run python -m agent.crons.renew_watch

refresh-token:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	set -a && source ../.env && set +a && \
	uv run python -m agent.dev.get_refresh_token

digest:
	cd src/agent && \
	source .venv/bin/activate && \
	set -a && source ../.env && set +a && \
	uv run python -m agent.crons.digest

quick-digest:
	cd src/agent && \
	source .venv/bin/activate && \
	set -a && source ../.env && set +a && \
	uv run python -m agent.crons.quick_digest

dashboard:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	uv run uvicorn agent.stats.dashboard:app --host 127.0.0.1 --port $(DASHBOARD_PORT) --reload

backfill:
	cd src/agent && \
	source .venv/bin/activate && \
	uv run python -m agent.stats.backfill

smoke:
	cd src/agent && \
	source .venv/bin/activate && \
	set -a && source ../.env && set +a && \
	uv run python -m agent.dev.smoke

test:
	cd src/agent && \
	uv sync && \
	source .venv/bin/activate && \
	uv run pytest -v


# ── AWS deployment ─────────────────────────────────────────────────────────────

deploy-bootstrap:
	aws cloudformation deploy \
		--template-file cloudformation/github-oidc.yml \
		--stack-name $(STACK_NAME)-github-oidc \
		--capabilities CAPABILITY_NAMED_IAM \
		--region $(AWS_REGION)
	@echo ""
	@echo "Add this as AWS_ROLE_ARN in your GitHub repository secrets:"
	@aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME)-github-oidc \
		--query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" \
		--output text \
		--region $(AWS_REGION)

secrets-create:
	@test -f cloudformation/secrets.json || ( \
		echo "ERROR: cloudformation/secrets.json not found." && \
		echo "  cp cloudformation/secrets-template.json cloudformation/secrets.json" && \
		echo "  # edit with real values" && \
		exit 1)
	@aws secretsmanager create-secret \
		--name $(SECRET_NAME) \
		--description "Email agent production credentials" \
		--region $(AWS_REGION) \
		--secret-string file://cloudformation/secrets.json 2>/dev/null || \
	aws secretsmanager put-secret-value \
		--secret-id $(SECRET_NAME) \
		--region $(AWS_REGION) \
		--secret-string file://cloudformation/secrets.json
	@echo "Secret created/updated: $(SECRET_NAME)"
	@echo ""
	@echo "Secret ARN (needed for deploy-infra if not cached):"
	@aws secretsmanager describe-secret \
		--secret-id $(SECRET_NAME) \
		--query ARN --output text --region $(AWS_REGION)

deploy-infra:
	@echo "--- Creating ECR repository (idempotent) ---"
	@aws ecr create-repository \
		--repository-name $(ECR_REPO) \
		--region $(AWS_REGION) \
		--image-scanning-configuration scanOnPush=true 2>/dev/null && \
		aws ecr put-lifecycle-policy \
			--repository-name $(ECR_REPO) \
			--region $(AWS_REGION) \
			--lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"Keep last 10","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":10},"action":{"type":"expire"}}]}' || \
		echo "ECR repository already exists, skipping."
	@echo "--- Deploying CloudFormation stack ---"
	$(eval SECRET_ARN := $(shell aws secretsmanager describe-secret \
		--secret-id $(SECRET_NAME) --query ARN --output text --region $(AWS_REGION)))
	aws cloudformation deploy \
		--template-file cloudformation/template.yml \
		--stack-name $(STACK_NAME) \
		--capabilities CAPABILITY_NAMED_IAM \
		--region $(AWS_REGION) \
		--no-fail-on-empty-changeset \
		--parameter-overrides \
			SecretArn=$(SECRET_ARN) \
			EcrImageUri=$(ECR_REGISTRY)/$(ECR_REPO):latest \
			TrustPhase=$(TRUST_PHASE) \
			EnableCrons=$(ENABLE_CRONS)

build:
	docker build --platform linux/amd64 -f src/Dockerfile -t $(ECR_REGISTRY)/$(ECR_REPO):latest src

push:
	aws ecr get-login-password --region $(AWS_REGION) | \
		docker login --username AWS --password-stdin $(ECR_REGISTRY)
	docker push $(ECR_REGISTRY)/$(ECR_REPO):latest

logs:
	aws logs tail /ecs/email-agent --follow --region $(AWS_REGION)

status:
	aws ecs describe-services \
		--cluster $(STACK_NAME) \
		--services $(STACK_NAME) \
		--region $(AWS_REGION) \
		--query "services[0].{Status:status,Running:runningCount,Pending:pendingCount,Desired:desiredCount}" \
		--output table

# ── Code quality ───────────────────────────────────────────────────────────────

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
