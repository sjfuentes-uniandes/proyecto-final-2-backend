"""Schemathesis 4 contra la app con los puertos en fakes (C01 nominal y reloj fijo)."""

from collections.abc import Iterator
from typing import Any

import pytest
import schemathesis
from cotizacion.config import Settings
from cotizacion.main import Puertos, crear_app
from fastapi.testclient import TestClient
from hypothesis import settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import response_schema_conformance


@pytest.fixture
def esquema(escenario: Any, perfil_c01: Any) -> Iterator[Any]:
    escenario.repo.perfiles[perfil_c01.id] = perfil_c01
    puertos = Puertos(
        identidad=escenario.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        ejecutor=escenario.ejecutor,
        perfiles=escenario.repo,
        reloj=escenario.reloj,
    )
    app = crear_app(Settings(), puertos=puertos)
    # El lifespan crea los casos de uso en app.state.
    with TestClient(app):
        yield schemathesis.openapi.from_asgi("/openapi.json", app)


schema = schemathesis.pytest.from_fixture("esquema")


@schema.parametrize()
@settings(max_examples=60, deadline=None)
def test_api_cumple_su_contrato(case: schemathesis.Case) -> None:
    case.call_and_validate(checks=[not_a_server_error, response_schema_conformance])
