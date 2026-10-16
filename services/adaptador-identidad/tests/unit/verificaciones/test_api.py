import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from adaptador_identidad.config import Settings
from adaptador_identidad.main import crear_app
from fastapi.testclient import TestClient

CLIENTE = "00000000-0000-4000-8000-000000000001"
CORRELACION = "corr-api-m06"

Manejador = Callable[[httpx.Request], httpx.Response]


def _nominal(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"resultado": "APROBADO", "motivo": None})


@pytest.fixture
def cliente_con() -> Iterator[Callable[[Manejador], tuple[TestClient, list[httpx.Request]]]]:
    abiertos: list[TestClient] = []

    def crear(manejador: Manejador) -> tuple[TestClient, list[httpx.Request]]:
        recibidas: list[httpx.Request] = []

        async def registrar(request: httpx.Request) -> httpx.Response:
            recibidas.append(request)
            respuesta = manejador(request)
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

        app = crear_app(Settings(), transport=httpx.MockTransport(registrar))
        cliente = TestClient(app, headers={"X-Correlation-Id": CORRELACION})
        cliente.__enter__()
        abiertos.append(cliente)
        return cliente, recibidas

    yield crear
    for cliente in abiertos:
        cliente.__exit__(None, None, None)


def _cuerpo(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "clienteId": CLIENTE,
        "selfie": "selfie-sintetica-aprobada",
        "pruebaDeVida": {"completado": True},
    }
    cuerpo.update(cambios)
    return cuerpo


def _error(respuesta: httpx.Response, estado: int, codigo: str) -> dict[str, Any]:
    assert respuesta.status_code == estado, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == codigo
    assert cuerpo["correlationId"] == CORRELACION
    return cuerpo


def test_200_aprobado(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(_nominal)
    respuesta = cliente.post("/v1/verificaciones", json=_cuerpo())
    assert respuesta.status_code == 200
    assert respuesta.json() == {"resultado": "APROBADO", "motivo": None}
    (request,) = recibidas
    assert request.headers["X-Api-Key"] == "local-sintetico"
    assert request.headers["X-Correlation-Id"] == CORRELACION


def test_200_captura_invalida(cliente_con: Any) -> None:
    cliente, _ = cliente_con(
        lambda _: httpx.Response(200, json={"resultado": "CAPTURA_INVALIDA", "motivo": "X"})
    )
    respuesta = cliente.post("/v1/verificaciones", json=_cuerpo())
    assert respuesta.json() == {"resultado": "CAPTURA_INVALIDA", "motivo": "X"}


@pytest.mark.parametrize(
    "cambios",
    [{"clienteId": "no-uuid"}, {"selfie": ""}],
)
def test_422_solicitud_invalida(cliente_con: Any, cambios: dict[str, Any]) -> None:
    cliente, recibidas = cliente_con(_nominal)
    respuesta = cliente.post("/v1/verificaciones", json=_cuerpo(**cambios))
    assert respuesta.status_code == 422
    assert recibidas == []


def test_504_tiempo_agotado(cliente_con: Any) -> None:
    cliente, _ = cliente_con(lambda r: httpx.ReadTimeout("lento", request=r))
    _error(cliente.post("/v1/verificaciones", json=_cuerpo()), 504, "TIEMPO_AGOTADO")


def test_503_fuente_no_disponible(cliente_con: Any) -> None:
    cliente, _ = cliente_con(lambda _: httpx.Response(503, json={"error": "unavailable"}))
    cuerpo = _error(cliente.post("/v1/verificaciones", json=_cuerpo()), 503, "FUENTE_NO_DISPONIBLE")
    assert cuerpo["detalles"] == {"motivo": "ERROR", "circuito": "CERRADO"}


def test_503_error_de_conexion(cliente_con: Any) -> None:
    cliente, _ = cliente_con(lambda r: httpx.ConnectError("caído", request=r))
    _error(cliente.post("/v1/verificaciones", json=_cuerpo()), 503, "FUENTE_NO_DISPONIBLE")


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(200, json={"resultado": "NO_EXISTE"}),
        httpx.Response(404),
        httpx.Response(200, text="<html>"),
    ],
)
def test_502_respuesta_invalida(cliente_con: Any, respuesta: httpx.Response) -> None:
    cliente, _ = cliente_con(lambda _: respuesta)
    _error(cliente.post("/v1/verificaciones", json=_cuerpo()), 502, "RESPUESTA_INVALIDA")


def test_el_timeout_configurado_no_se_recorta_a_700_ms() -> None:
    """Una verificación biométrica incluye al proveedor KYC: su timeout es de 3 s, no de 700 ms."""
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json={"resultado": "APROBADO", "motivo": None})

    app = crear_app(Settings(), transport=httpx.MockTransport(manejador))
    with TestClient(app) as cliente:
        assert cliente.post("/v1/verificaciones", json=_cuerpo()).status_code == 200
    assert recibidas[0].extensions["timeout"]["read"] == 3.0


def test_health_sin_dependencias(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(_nominal)
    assert cliente.get("/health").json() == {"status": "ok"}
    assert recibidas == []


def test_el_lifespan_cierra_el_cliente() -> None:
    app = crear_app(Settings(), transport=httpx.MockTransport(_nominal))
    with TestClient(app):
        caso = app.state.verificar_identidad
    assert caso._proveedor._cliente.is_closed
    assert asyncio.iscoroutinefunction(caso.ejecutar)
