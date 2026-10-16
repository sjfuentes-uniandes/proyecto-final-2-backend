"""Schemathesis 4 contra la app con una app real que nunca contacta a `clientes`.

Sin un JWT firmado válido, la autenticación siempre rechaza con 401: el objetivo aquí es
que la API nunca responda con un esquema distinto del declarado, no recorrer la autenticación
(cubierta en tests/unit/sesion). Un transporte que falla asegura que, si alguna vez se
alcanzara la llamada a `clientes`, no haya tráfico de red real.
"""

import httpx
import schemathesis
from bff_movil.config import Settings
from bff_movil.main import crear_app
from fastapi.testclient import TestClient
from hypothesis import settings

VERIFICACION_NOMINAL = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "estado": "VERIFICADO",
    "resultado": "APROBADO",
    "motivo": None,
    "verificadoEn": "2026-10-01T00:00:00Z",
    "tokenRegistro": "v1.token-sintetico.firma-sintetica",
    "expiraEn": "2026-10-01T00:30:00Z",
}


# POST /v1/registro/verificaciones es publica: no pasa por la autenticacion y si alcanza a
# `clientes`, que aqui responde una verificacion nominal. Cualquier otra llamada falla.
def _clientes_simulado(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path.endswith("/verificaciones"):
        return httpx.Response(200, json=VERIFICACION_NOMINAL)
    raise httpx.ConnectError("Schemathesis no debe alcanzar a clientes")


app = crear_app(Settings(environment="pruebas"), transport=httpx.MockTransport(_clientes_simulado))
# Arranca el lifespan una vez: crea los clientes HTTP y el caso de uso en app.state.
_ciclo = TestClient(app)
_ciclo.__enter__()

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


@schema.parametrize()
@settings(max_examples=30, deadline=None)
def test_api_cumple_su_contrato(case: schemathesis.Case) -> None:
    case.call_and_validate()
