UV ?= uv
PY := $(UV) run python

.PHONY: setup test lint fmt hygiene sample fetch index stats cone plan seed dev api web web-build docker compose-up compose-down k8s-validate

setup: ## install Python deps into .venv and web deps into web/node_modules
	$(UV) sync
	npm --prefix web install

test: ## full offline test suite ($0, no network)
	$(UV) run pytest

lint:
	$(UV) run ruff check .

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

hygiene: ## fail if any tracked file is licensed data, local state or a secret
	python3 scripts/check_repo_hygiene.py

sample: ## synthetic mini Lean tree for instant local testing
	$(PY) scripts/fetch_riemann_archive.py --sample

fetch: ## resumable, checksum-verified download of the 14-part archive + p0 stats + remark.pdf
	$(PY) scripts/fetch_riemann_archive.py --p0 --pdf

index: ## build the $0 structural index from the extracted archive
	$(PY) -m eddie.cli index build --src data/raw/extracted

stats:
	$(PY) -m eddie.cli index stats

cone: ## $0 declaration graph, live proof cone, pillars and zoom tree (needs the index)
	$(PY) -m eddie.cli cone build

plan: ## exact tiered Ember work plan inside $$6.50 ($0: no model calls)
	$(PY) -m eddie.cli plan-budget --target-usd 6.50

seed: ## $0 sandbox witnesses for the explorer, labeled seed (never Ember output)
	$(PY) -m eddie.cli seed-demo

dev: ## API on 127.0.0.1:8765 + Vite on 127.0.0.1:5173; Ctrl-C stops both
	@trap 'kill 0' INT TERM EXIT; \
	$(UV) run uvicorn eddie.api.app:app --host 127.0.0.1 --port 8765 & \
	npm --prefix web run dev & \
	wait

api: ## read-only API on 127.0.0.1:8765
	$(UV) run uvicorn eddie.api.app:app --host 127.0.0.1 --port 8765

web: ## Vite dev server on :5173 (proxies /api to :8765)
	npm --prefix web run dev

web-build:
	npm --prefix web run build

docker:
	docker build -t eddie:dev .

compose-up: ## keyless read-only API container on 127.0.0.1:8765
	install -d -m 700 "$${HOME}/.local/share/eddie" "$${HOME}/.local/share/eddie/public"
	mkdir -p data/index
	docker compose up -d --build

compose-down:
	docker compose down

k8s-validate: ## render + client-side validate only; never applies
	kubectl kustomize k8s | kubectl apply --dry-run=client -f -
