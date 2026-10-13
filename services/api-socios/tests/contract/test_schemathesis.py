"""Schemathesis sobre contracts/openapi/api-socios.yaml contra la app (dobles en memoria).

Fuzz de todas las operaciones más casos explícitos para 201/400/401/403 (HU-W01 N2).
"""

from pathlib import Path

import pytest
import schemathesis
from api_socios.main import app
from hypothesis import HealthCheck, settings

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "api-socios.yaml"
schema = schemathesis.openapi.from_path(CONTRATO)
schema.app = app  # las llamadas van a la app ASGI


@pytest.fixture(autouse=True, scope="module")
def dobles(entorno_compartido):
    return entorno_compartido


@schema.parametrize()
@settings(max_examples=25, deadline=None, suppress_health_check=list(HealthCheck))
def test_contrato_api_socios(case: schemathesis.Case) -> None:
    # El 405 de Starlette solo lista en Allow los métodos de la primera ruta que coincide
    # (comportamiento del framework, no de HU-W01).
    case.call_and_validate(excluded_checks=[schemathesis.checks.allow_header_conformance])


ALTA = {"nombre": "Fintech Caribe S.A.S.", "canal": "plataforma_digital", "plan": "basico", "permisos": ["cotizar"]}


def _llamar(metodo: str, ruta: str, **kwargs: object):
    caso = schema[ruta][metodo].Case(**kwargs)
    respuesta = caso.call()
    caso.validate_response(respuesta)
    return respuesta


def test_codigos_de_la_historia_cumplen_el_contrato(dobles) -> None:
    assert _llamar("POST", "/admin/socios", body=ALTA, media_type="application/json").status_code == 201
    assert _llamar("POST", "/admin/socios", body=ALTA | {"permisos": []},
                   media_type="application/json").status_code == 400
    assert _llamar("GET", "/socio/me", headers={"X-Partner-Client-Id": "desconocido"}).status_code == 401

    socio = next(s for s in dobles.repositorio.listar() if s.permisos)
    socio_headers = {"X-Partner-Client-Id": socio.client_id, "X-Partner-Key-Id": socio.api_key_id,
                     "X-Partner-Scopes": "solventa/cotizar"}
    assert _llamar("GET", "/socio/me", headers=socio_headers).status_code == 200
    assert _llamar("POST", "/polizas", headers=socio_headers).status_code == 403
