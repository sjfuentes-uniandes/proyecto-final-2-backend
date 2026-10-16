"""Schemathesis 4 contra la app con el repositorio en memoria y el reloj fijo."""

from collections.abc import Iterator
from typing import Any

import pytest
import schemathesis
from catalogo.config import Settings
from catalogo.main import crear_app
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import response_schema_conformance


@pytest.fixture
def esquema(repositorio: Any, reloj: Any) -> Iterator[Any]:
    app = crear_app(Settings(), repositorio=repositorio, reloj=reloj)
    # El lifespan crea los casos de uso en app.state.
    with TestClient(app):
        yield schemathesis.openapi.from_asgi("/openapi.json", app)


schema = schemathesis.pytest.from_fixture("esquema")


@schema.parametrize()
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
def test_api_cumple_su_contrato(case: schemathesis.Case) -> None:
    case.call_and_validate(checks=[not_a_server_error, response_schema_conformance])
