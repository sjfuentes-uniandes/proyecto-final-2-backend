"""Schemathesis 4 contra la app con SinProteccion y el proveedor simulado."""

import json
from urllib.parse import parse_qs

import httpx
import schemathesis
from adaptador_pagos.config import Settings
from adaptador_pagos.main import crear_app
from fastapi.testclient import TestClient
from hypothesis import settings
from schemathesis.specs.openapi.checks import positive_data_acceptance


def _nominal(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        referencia = json.loads(request.content)["referencia"]
    else:
        referencia = parse_qs(request.url.query.decode())["referencia"][0]
    cuerpo = {
        "referencia": referencia,
        "estado": "APROBADO",
        "transaccionId": "TX-SINT-C01",
        "motivo": None,
    }
    return httpx.Response(200, json=cuerpo)


app = crear_app(Settings(), transport=httpx.MockTransport(_nominal))
# Arranca el lifespan una vez: crea el cliente y los casos de uso en app.state.
_ciclo = TestClient(app)
_ciclo.__enter__()

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


# positive_data_acceptance se excluye: Schemathesis no conoce la regla de negocio detrás
# del 422 (monto mayor que 0; SOLICITUD_INVALIDA ya la cubre test_api.py).
@schema.parametrize()
@settings(max_examples=60, deadline=None)
def test_api_cumple_su_contrato(case: schemathesis.Case) -> None:
    case.call_and_validate(excluded_checks=[positive_data_acceptance])
