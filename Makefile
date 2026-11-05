.PHONY: install backend frontend dev test lint build clean

install: ## Install all dependencies
	cd backend && uv sync
	cd frontend && npm install

backend: ## Run the API server (http://localhost:8000, docs at /docs)
	cd backend && uv run uvicorn app.main:app --reload --port 8000

frontend: ## Run the web UI (http://localhost:5173, proxies /api to :8000)
	cd frontend && npm run dev

test: ## Backend test suite
	cd backend && uv run pytest -q

test-frontend: ## Frontend store tests (vitest)
	cd frontend && npm run test

lint: ## Backend lint
	cd backend && uv run ruff check .

build: ## Production build of the frontend
	cd frontend && npm run build

clean: ## Remove build artifacts and local data
	rm -rf frontend/dist backend/data
