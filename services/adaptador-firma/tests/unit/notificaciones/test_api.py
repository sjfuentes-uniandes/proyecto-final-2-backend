"""API POST /v1/notificaciones: cada correspondencia de errores, el rechazo y los logs."""

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from adaptador_firma.adapters.inbound.http.notificaciones import SolicitudPush
from adaptador_firma.config import Settings
from adaptador_firma.main import crear_app
from fastapi.testclient import TestClient

TOKEN = "token-sintetico-entregado"
POLIZA = "5b1c9e2a-7f3d-4a60-8c11-000000000d01"
EVENTO = "00000000-0000-4000-8000-000000000e01"
CORRELACION = "corr-api-m11"

Manejador = Callable[[httpx.Request], httpx.Response | Exception]
_FALTA = object()


def _aceptado(request: httpx.Request, **cambios: Any) -> httpx.Response:
    cuerpo: dict[str, Any] = {"estado": "ENVIADO", "envioId": "PUSH-1"}
    cuerpo.update(cambios)
    return httpx.Response(200, json=cuerpo)


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
        "tokenDispositivo": TOKEN,
        "tipo": "POLIZA_EMITIDA",
        "polizaId": POLIZA,
        "eventId": EVENTO,
    }
    cuerpo.update(cambios)
    return {clave: valor for clave, valor in cuerpo.items() if valor is not _FALTA}


def _notificar(cliente: TestClient, **cambios: Any) -> httpx.Response:
    return cliente.post("/v1/notificaciones", json=_cuerpo(**cambios))


def _error(respuesta: httpx.Response, estado: int, codigo: str) -> dict[str, Any]:
    assert respuesta.status_code == estado, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == codigo
    assert cuerpo["correlationId"] == CORRELACION
    return cuerpo


# --- 200: aceptado y rechazado ----------------------------------------------------------------
def test_aceptado(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(_aceptado)
    respuesta = _notificar(cliente)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {"resultado": "OK", "envioId": "PUSH-1"}
    (request,) = recibidas
    assert request.method == "POST"
    assert request.url.path == "/push/v1/envios"
    assert request.headers["X-Api-Key"] == "local-sintetico"
    assert request.headers["X-Correlation-Id"] == CORRELACION
    assert json.loads(request.content) == {
        "tokenDispositivo": TOKEN,
        "tipo": "POLIZA_EMITIDA",
        "polizaId": POLIZA,
        "eventId": EVENTO,
    }


def test_rechazado_por_token_no_reconocido(cliente_con: Any) -> None:
    cliente, _ = cliente_con(lambda _: httpx.Response(404, json={"error": "token_no_reconocido"}))
    respuesta = _notificar(cliente)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {"resultado": "RECHAZADO", "envioId": None}


# --- 422: entrada inválida, sin llamar al proveedor --------------------------------------------
@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"tokenDispositivo": ""}, "tokenDispositivo"),
        ({"tokenDispositivo": _FALTA}, "tokenDispositivo"),
        ({"tipo": _FALTA}, "tipo"),
        ({"polizaId": "no-uuid"}, "polizaId"),
        ({"polizaId": _FALTA}, "polizaId"),
        ({"eventId": "no-uuid"}, "eventId"),
        ({"eventId": _FALTA}, "eventId"),
    ],
)
def test_422_solicitud_invalida(cliente_con: Any, cambios: dict[str, Any], campo: str) -> None:
    cliente, recibidas = cliente_con(_aceptado)
    cuerpo = _error(_notificar(cliente, **cambios), 422, "SOLICITUD_INVALIDA")
    assert cuerpo["detalles"]["campos"] == [campo]
    assert recibidas == []


# --- Correspondencia de los fallos del proveedor ------------------------------------------------
def test_504_tiempo_agotado(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(lambda r: httpx.ReadTimeout("lento", request=r))
    _error(_notificar(cliente), 504, "TIEMPO_AGOTADO")
    assert len(recibidas) == 1


@pytest.mark.parametrize(
    "manejador",
    [
        pytest.param(lambda _: httpx.Response(503, json={"error": "unavailable"}), id="503"),
        pytest.param(lambda _: httpx.Response(500), id="500"),
        pytest.param(lambda r: httpx.ConnectError("caído", request=r), id="conexion"),
    ],
)
def test_503_fuente_no_disponible(cliente_con: Any, manejador: Manejador) -> None:
    cliente, recibidas = cliente_con(manejador)
    cuerpo = _error(_notificar(cliente), 503, "FUENTE_NO_DISPONIBLE")
    assert cuerpo["detalles"] == {"motivo": "ERROR", "circuito": "CERRADO"}
    assert len(recibidas) == 1


@pytest.mark.parametrize(
    "manejador",
    [
        pytest.param(lambda _: httpx.Response(200, text='{"estado": '), id="malformado"),
        pytest.param(
            lambda _: httpx.Response(200, json={"estado": "RECHAZADO"}), id="fuera-de-forma"
        ),
        pytest.param(lambda _: httpx.Response(404, json={"error": "otra_cosa"}), id="404-distinto"),
        pytest.param(lambda _: httpx.Response(404, text="no soy json"), id="404-no-json"),
        pytest.param(lambda _: httpx.Response(400, json={"error": "x"}), id="400"),
        pytest.param(lambda _: httpx.Response(409), id="409"),
    ],
)
def test_502_respuesta_invalida(cliente_con: Any, manejador: Manejador) -> None:
    cliente, recibidas = cliente_con(manejador)
    cuerpo = _error(_notificar(cliente), 502, "RESPUESTA_INVALIDA")
    assert cuerpo["detalles"] == {}
    assert len(recibidas) == 1


# --- El token no va en logs ni en errores -------------------------------------------------------
def test_el_repr_del_dto_no_muestra_el_token() -> None:
    solicitud = SolicitudPush(
        token_dispositivo=TOKEN, tipo="POLIZA_EMITIDA", poliza_id=POLIZA, event_id=EVENTO
    )
    assert TOKEN not in repr(solicitud)
    assert TOKEN not in str(solicitud)


def test_el_token_no_aparece_en_la_respuesta_de_error(cliente_con: Any) -> None:
    cliente, _ = cliente_con(lambda _: httpx.Response(500))
    respuesta = _notificar(cliente)
    assert TOKEN not in respuesta.text


def test_el_token_no_aparece_en_los_logs(
    monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[..., Any]
) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    app = crear_app(Settings(), transport=httpx.MockTransport(lambda r: _aceptado(r)))
    salida = capturar_logs()  # después de crear la app: instrumentar() reconfigura el logging
    with TestClient(app) as cliente:
        cliente.post("/v1/notificaciones", json=_cuerpo())
    texto = salida.getvalue()
    assert '"operation": "notificacion.push"' in texto
    assert TOKEN not in texto
