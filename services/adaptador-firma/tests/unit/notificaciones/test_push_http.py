"""Cliente del proveedor push (PushHttp) contra respx: encabezados, cuerpo y cada respuesta."""

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from adaptador_firma.adapters.outbound.push_http import PushHttp
from adaptador_firma.domain.notificaciones.modelos import (
    Push,
    RespuestaInvalida,
    ResultadoPush,
    SolicitudPush,
)
from pydantic import SecretStr

pytestmark = pytest.mark.anyio

BASE = "http://sim/push"
POLIZA = UUID("5b1c9e2a-7f3d-4a60-8c11-000000000d01")
EVENTO = UUID("00000000-0000-4000-8000-000000000e01")
API_KEY = "clave-sintetica-push"
SOLICITUD = SolicitudPush(
    token_dispositivo="token-sintetico-entregado",
    tipo="POLIZA_EMITIDA",
    poliza_id=POLIZA,
    event_id=EVENTO,
)


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


def _proveedor() -> PushHttp:
    return PushHttp(httpx.AsyncClient(), BASE, SecretStr(API_KEY))


async def _enviar(solicitud: SolicitudPush = SOLICITUD) -> Push:
    return await _proveedor().enviar(solicitud, 2.0)


async def test_envia_encabezados_y_cuerpo_exactos(mock: respx.MockRouter) -> None:
    ruta = mock.post("/v1/envios").respond(200, json={"estado": "ENVIADO", "envioId": "PUSH-1"})
    push = await _enviar()
    assert push == Push(resultado=ResultadoPush.OK, envio_id="PUSH-1")
    request = ruta.calls.last.request
    assert request.url == f"{BASE}/v1/envios"
    assert request.headers["X-Api-Key"] == API_KEY
    assert "Idempotency-Key" not in request.headers
    assert request.extensions["timeout"]["read"] == 2.0
    assert json.loads(request.content) == {
        "tokenDispositivo": "token-sintetico-entregado",
        "tipo": "POLIZA_EMITIDA",
        "polizaId": str(POLIZA),
        "eventId": str(EVENTO),
    }


async def test_envioId_es_opcional(mock: respx.MockRouter) -> None:
    mock.post("/v1/envios").respond(200, json={"estado": "ENVIADO"})
    assert await _enviar() == Push(resultado=ResultadoPush.OK, envio_id=None)


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("lento"), httpx.ConnectTimeout("lento"), httpx.ConnectError("caído")],
)
async def test_timeout_y_conexion_se_propagan(mock: respx.MockRouter, error: Exception) -> None:
    mock.post("/v1/envios").mock(side_effect=error)
    with pytest.raises(type(error)):
        await _enviar()


@pytest.mark.parametrize("estado", [500, 502, 503, 504])
async def test_5xx_lanza_http_status_error(mock: respx.MockRouter, estado: int) -> None:
    mock.post("/v1/envios").respond(estado, json={"error": "unavailable"})
    with pytest.raises(httpx.HTTPStatusError):
        await _enviar()


async def test_404_token_no_reconocido_es_rechazado(mock: respx.MockRouter) -> None:
    mock.post("/v1/envios").respond(404, json={"error": "token_no_reconocido"})
    assert await _enviar() == Push(resultado=ResultadoPush.RECHAZADO, envio_id=None)


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"error": "otra_cosa"},
        {"error": "token_no_reconocido", "extra": True},
        {},
        {"mensaje": "no encontrado"},
    ],
)
async def test_404_que_no_es_token_no_reconocido_es_invalida(
    mock: respx.MockRouter, cuerpo: Any
) -> None:
    mock.post("/v1/envios").respond(404, json=cuerpo)
    with pytest.raises(RespuestaInvalida) as error:
        await _enviar()
    assert error.value.motivo == "HTTP_404"


async def test_404_sin_cuerpo_json_es_invalida(mock: respx.MockRouter) -> None:
    mock.post("/v1/envios").respond(404, text="no soy json")
    with pytest.raises(RespuestaInvalida) as error:
        await _enviar()
    assert error.value.motivo == "HTTP_404"


@pytest.mark.parametrize("texto", ['{"estado": ', "<html>", ""])
async def test_cuerpo_malformado(mock: respx.MockRouter, texto: str) -> None:
    mock.post("/v1/envios").respond(200, text=texto)
    with pytest.raises(RespuestaInvalida) as error:
        await _enviar()
    assert error.value.motivo == "JSON"


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"estado": "RECHAZADO"},
        {"estado": "enviado"},
        {},
        {"estado": "ENVIADO", "envioId": ""},
        {"estado": "ENVIADO", "envioId": "   "},
        {"estado": "ENVIADO", "envioId": 1},
    ],
)
async def test_cuerpo_fuera_de_forma(mock: respx.MockRouter, cuerpo: Any) -> None:
    mock.post("/v1/envios").respond(200, json=cuerpo)
    with pytest.raises(RespuestaInvalida) as error:
        await _enviar()
    assert error.value.motivo == "JSON"


@pytest.mark.parametrize("estado", [201, 204, 302, 400, 401, 409, 422])
async def test_otro_estado_es_respuesta_invalida(mock: respx.MockRouter, estado: int) -> None:
    mock.post("/v1/envios").respond(estado, json={"estado": "ENVIADO"})
    with pytest.raises(RespuestaInvalida) as error:
        await _enviar()
    assert error.value.motivo == f"HTTP_{estado}"


async def test_el_repr_de_la_solicitud_no_muestra_el_token() -> None:
    assert "token-sintetico-entregado" not in repr(SOLICITUD)
    assert "token-sintetico-entregado" not in str(SOLICITUD)
