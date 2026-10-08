include infra/plataforma.mk

.PHONY: dev-up dev-down dev-sync test-unit test-module test-contract test-e2e diagramas

SERVICE ?=

dev-sync:
	uv sync --all-packages

dev-up:
	docker compose up -d --build

dev-down:
	docker compose down

test-unit:
	uv run pytest $(if $(SERVICE),services/$(SERVICE)/tests/unit,services/*/tests/unit libs/*/tests)

test-module:
	uv run pytest services/$(SERVICE)/tests/module

test-contract:
	uv run pytest services/$(SERVICE)/tests/contract

test-e2e:
	uv run pytest tests/e2e

diagramas:
	@./scripts/generate_diagrams.sh
