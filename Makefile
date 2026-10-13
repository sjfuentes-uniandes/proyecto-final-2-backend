include infra/plataforma.mk

.PHONY: dev-up dev-down dev-sync lint test-unit test-cov test-module test-contract test-e2e diagramas

SERVICE ?=

# Código 5 de pytest = no se recolectaron pruebas: cuenta como éxito en module y contract.
PYTEST_SIN_PRUEBAS_OK = s=$$?; if [ $$s -eq 5 ]; then exit 0; else exit $$s; fi

dev-sync:
	uv sync --all-packages

dev-up:
	docker compose up -d --build

dev-down:
	docker compose down

lint:
	uv run ruff check services/$(SERVICE) && uv run ruff format --check services/$(SERVICE)

test-unit:
	uv run pytest $(if $(SERVICE),services/$(SERVICE)/tests/unit,services/*/tests/unit libs/*/tests/unit)

test-cov:
	uv run pytest services/$(SERVICE)/tests/unit --cov=services/$(SERVICE)/src --cov-branch \
		--cov-report=term --cov-report=json:services/$(SERVICE)/cobertura.json \
		&& uv run python scripts/calidad/cobertura.py $(SERVICE) services/$(SERVICE)/cobertura.json

test-module:
	uv run pytest $(if $(SERVICE),services/$(SERVICE)/tests/module,libs/*/tests/module); $(PYTEST_SIN_PRUEBAS_OK)

test-contract:
	uv run pytest services/$(SERVICE)/tests/contract; $(PYTEST_SIN_PRUEBAS_OK)

test-e2e:
	uv run pytest tests/e2e

diagramas:
	@./scripts/generate_diagrams.sh
