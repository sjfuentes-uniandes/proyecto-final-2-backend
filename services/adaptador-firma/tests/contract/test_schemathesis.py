"""Schemathesis 4 contra la app con SinProteccion y el proveedor simulado."""

import json

import httpx
import schemathesis
from adaptador_firma.config import Settings
from adaptador_firma.main import crear_app
from fastapi.testclient import TestClient
from hypothesis import settings


def _firmado(request: httpx.Request) -> httpx.Response:
    enviado = json.loads(request.content)
    cuerpo = {
        "documentoId": enviado["documentoId"],
        "estado": "FIRMADO",
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": "2026-01-01T00:00:00Z",
        "huella": enviado["huella"],
    }
    return httpx.Response(200, json=cuerpo)


def _push_enviado(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"estado": "ENVIADO", "envioId": "PUSH-SINT-0001"})


def _proveedor(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/firma/v1/firmas":
        return _firmado(request)
    return _push_enviado(request)


app = crear_app(Settings(), transport=httpx.MockTransport(_proveedor))
# Arranca el lifespan una vez: crea el cliente y el caso de uso en app.state.
_ciclo = TestClient(app)
_ciclo.__enter__()

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


@schema.parametrize()
@settings(max_examples=60, deadline=None)
def test_api_cumple_su_contrato(case: schemathesis.Case) -> None:
    case.call_and_validate()
