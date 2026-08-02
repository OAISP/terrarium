.DEFAULT_GOAL := help
SHELL := /bin/bash

IMAGE ?= terrarium-sandbox
NETWORK ?= terrarium-net

.PHONY: help setup build network redteam redteam-pinhole redteam-conceal test lint run skills web clean image-shell

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Install host deps (orchestrator needs the server extra)
	uv sync --extra server

build: ## Build the hardened sandbox image
	docker build -f sandbox/Dockerfile -t $(IMAGE) .

network: ## Ensure the isolated docker network exists
	@docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK)

test: ## Run fast unit tests (no docker / no API calls)
	# Every suite the pipeline runs. Each discovers its own test_* functions (tests/_runner.py),
	# so a new test is live the moment it's written.
	uv run python tests/test_unit.py
	uv run python tests/test_sdk.py
	uv run python tests/test_worker_rewind.py
	cd warden && cargo test --quiet

lint: ## Lint + typecheck everything (python · rust · console)
	uv run ruff check .
	# The lockfile is the only record of what the suite actually ran against; CI installs with
	# pip, so nothing else notices when a pyproject floor bump isn't re-locked.
	uv lock --check
	cd warden && cargo clippy --all-targets -- -D warnings
	cd web && bunx tsc --noEmit && bun run lint

redteam: network ## Red-team the sandbox isolation boundary (needs the image)
	uv run python tests/redteam_isolation.py

redteam-pinhole: network ## Red-team the Tier-2 firewall pinhole
	uv run python tests/redteam_pinhole.py

redteam-conceal: network ## Red-team sandbox concealment (no env tells, combined CA store)
	uv run python tests/redteam_conceal.py

run: network ## Run the orchestrator API (localhost:8900)
	uv run terra

skills: ## List skills (use scripts/skill.sh new <name> to scaffold)
	@scripts/skill.sh list

web: ## Run the web dashboard (localhost:3737)
	cd web && bun install && bun run dev

image-shell: ## Open a shell in the sandbox image (firewall active, as agent)
	docker run -it --rm $$(uv run python -c "from orchestrator.runners import hardened_flags; print(' '.join(hardened_flags('$(NETWORK)')))") $(IMAGE) sh

clean: ## Remove leftover session containers, workspace volumes, and the egress proxy
	-docker ps -aq --filter "name=terrarium-session-" | xargs -r docker rm -f
	-docker rm -f terrarium-egress-proxy terrarium-pinhole-test 2>/dev/null
	-docker volume ls -q --filter "name=terrarium-ws-" | xargs -r docker volume rm -f
