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


def _sin_red(_: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("Schemathesis no debe alcanzar a clientes")


app = crear_app(Settings(environment="pruebas"), transport=httpx.MockTransport(_sin_red))
# Arranca el lifespan una vez: crea los clientes HTTP y el caso de uso en app.state.
_ciclo = TestClient(app)
_ciclo.__enter__()

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


@schema.parametrize()
@settings(max_examples=30, deadline=None)
def test_api_cumple_su_contrato(case: schemathesis.Case) -> None:
    case.call_and_validate()
