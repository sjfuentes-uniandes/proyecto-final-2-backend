"""Cliente del aliado de pagos para desembolsos (DesembolsosHttp) contra respx (HU-W23)."""

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from adaptador_pagos.adapters.outbound.desembolsos_http import DesembolsosHttp
from adaptador_pagos.domain.cobros.modelos import RespuestaInvalida
from adaptador_pagos.domain.desembolsos.modelos import (
    Desembolso,
    DesembolsoNoEncontrado,
    EstadoDesembolso,
    SolicitudDesembolso,
)
from pydantic import SecretStr

pytestmark = pytest.mark.anyio

BASE = "http://sim/pagos"
REFERENCIA = "IND-88-2026-0442-01"
BENEFICIARIO = "ben-sint-001"
API_KEY = "clave-sintetica-pagos"
SOLICITUD = SolicitudDesembolso(
    referencia=REFERENCIA, monto=Decimal("2500000.00"), moneda="COP", beneficiario_ref=BENEFICIARIO
)
RUTAS = ["desembolso", "consulta"]


def _respuesta(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "referencia": REFERENCIA,
        "estado": "CONFIRMADO",
        "transaccionId": "TRX-SINT-1",
        "motivo": None,
    }
    cuerpo.update(cambios)
    return cuerpo


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


def _proveedor() -> DesembolsosHttp:
    return DesembolsosHttp(httpx.AsyncClient(), BASE + "/", SecretStr(API_KEY))


async def _llamar(ruta: str) -> Desembolso:
    if ruta == "desembolso":
        return await _proveedor().desembolsar(SOLICITUD, 5.0)
    return await _proveedor().consultar(REFERENCIA, BENEFICIARIO, 5.0)


async def test_desembolso_envia_encabezados_y_cuerpo_exactos(mock: respx.MockRouter) -> None:
    ruta = mock.post("/v1/desembolsos").respond(200, json=_respuesta())
    desembolso = await _proveedor().desembolsar(SOLICITUD, 5.0)
    assert desembolso == Desembolso(
        referencia=REFERENCIA,
        estado=EstadoDesembolso.CONFIRMADO,
        transaccion_id="TRX-SINT-1",
        motivo=None,
    )
    request = ruta.calls.last.request
    assert request.url == f"{BASE}/v1/desembolsos"
    assert request.headers["Idempotency-Key"] == REFERENCIA
    assert request.headers["X-Api-Key"] == API_KEY
    assert json.loads(request.content) == {
        "referencia": REFERENCIA,
        "monto": "2500000.00",
        "moneda": "COP",
        "beneficiario": BENEFICIARIO,
    }
    assert request.extensions["timeout"]["read"] == 5.0


async def test_desembolso_rechazado_con_motivo(mock: respx.MockRouter) -> None:
    mock.post("/v1/desembolsos").respond(
        200,
        json=_respuesta(
            estado="RECHAZADO", transaccionId=None, motivo="BENEFICIARIO_NO_HABILITADO"
        ),
    )
    assert await _proveedor().desembolsar(SOLICITUD, 5.0) == Desembolso(
        referencia=REFERENCIA,
        estado=EstadoDesembolso.RECHAZADO,
        transaccion_id=None,
        motivo="BENEFICIARIO_NO_HABILITADO",
    )


async def test_consulta_envia_referencia_beneficiario_y_api_key(mock: respx.MockRouter) -> None:
    ruta = mock.get("/v1/desembolsos").respond(200, json=_respuesta())
    desembolso = await _proveedor().consultar(REFERENCIA, BENEFICIARIO, 5.0)
    assert (desembolso.estado, desembolso.transaccion_id) == (
        EstadoDesembolso.CONFIRMADO,
        "TRX-SINT-1",
    )
    request = ruta.calls.last.request
    assert request.url.path == "/pagos/v1/desembolsos"
    assert dict(request.url.params) == {"referencia": REFERENCIA, "beneficiario": BENEFICIARIO}
    assert request.headers["X-Api-Key"] == API_KEY
    assert "Idempotency-Key" not in request.headers
    assert request.content == b""


async def test_consulta_rechazada(mock: respx.MockRouter) -> None:
    mock.get("/v1/desembolsos").respond(
        200, json=_respuesta(estado="RECHAZADO", transaccionId=None, motivo="X_MOTIVO")
    )
    desembolso = await _proveedor().consultar(REFERENCIA, BENEFICIARIO, 5.0)
    assert (desembolso.estado, desembolso.motivo) == (EstadoDesembolso.RECHAZADO, "X_MOTIVO")


async def test_consulta_404_desembolso_no_encontrado(mock: respx.MockRouter) -> None:
    mock.get("/v1/desembolsos").respond(404, json={"error": "desembolso_no_encontrado"})
    with pytest.raises(DesembolsoNoEncontrado):
        await _proveedor().consultar(REFERENCIA, BENEFICIARIO, 5.0)


@pytest.mark.parametrize("ruta", RUTAS)
@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("lento"), httpx.ConnectTimeout("lento"), httpx.ConnectError("caído")],
)
async def test_timeout_y_conexion_se_propagan(
    mock: respx.MockRouter, ruta: str, error: Exception
) -> None:
    mock.route(path="/v1/desembolsos").mock(side_effect=error)
    with pytest.raises(type(error)):
        await _llamar(ruta)


@pytest.mark.parametrize("ruta", RUTAS)
@pytest.mark.parametrize("estado", [500, 502, 503, 504])
async def test_5xx_lanza_http_status_error(mock: respx.MockRouter, ruta: str, estado: int) -> None:
    mock.route(path="/v1/desembolsos").respond(estado, json={"error": "unavailable"})
    with pytest.raises(httpx.HTTPStatusError):
        await _llamar(ruta)


@pytest.mark.parametrize("ruta", RUTAS)
@pytest.mark.parametrize(
    "respuesta",
    [
        pytest.param(httpx.Response(404, json={"error": "referencia_no_reconocida"}), id="404"),
        pytest.param(httpx.Response(400, json={"error": "x"}), id="400"),
        pytest.param(httpx.Response(302), id="302"),
    ],
)
async def test_otro_estado_es_respuesta_invalida(
    mock: respx.MockRouter, ruta: str, respuesta: httpx.Response
) -> None:
    mock.route(path="/v1/desembolsos").mock(return_value=respuesta)
    with pytest.raises(RespuestaInvalida):
        await _llamar(ruta)


@pytest.mark.parametrize("ruta", RUTAS)
@pytest.mark.parametrize(
    "cuerpo",
    [
        pytest.param(_respuesta(referencia="IND-88-2026-9999-01"), id="otra-referencia"),
        pytest.param(_respuesta(estado="APROBADO"), id="estado-desconocido"),
        pytest.param(_respuesta(estado="PENDIENTE"), id="estado-no-previsto"),
        pytest.param(_respuesta(transaccionId=None), id="confirmado-sin-transaccion"),
        pytest.param(_respuesta(transaccionId=""), id="transaccion-vacia"),
        pytest.param(_respuesta(transaccionId=5), id="transaccion-no-texto"),
        pytest.param(_respuesta(motivo=""), id="motivo-vacio"),
        pytest.param({"estado": "CONFIRMADO"}, id="sin-referencia"),
        pytest.param([1, 2], id="no-es-objeto"),
    ],
)
async def test_cuerpo_fuera_de_contrato_es_respuesta_invalida(
    mock: respx.MockRouter, ruta: str, cuerpo: Any
) -> None:
    mock.route(path="/v1/desembolsos").respond(200, json=cuerpo)
    with pytest.raises(RespuestaInvalida):
        await _llamar(ruta)


@pytest.mark.parametrize("ruta", RUTAS)
async def test_json_malformado_es_respuesta_invalida(mock: respx.MockRouter, ruta: str) -> None:
    mock.route(path="/v1/desembolsos").respond(200, text='{"estado": ')
    with pytest.raises(RespuestaInvalida):
        await _llamar(ruta)
