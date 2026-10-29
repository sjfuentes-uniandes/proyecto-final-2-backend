"""Cliente del proveedor de pagos (PagosHttp) contra respx: encabezados, cuerpo y cada respuesta."""

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from adaptador_pagos.adapters.outbound.pagos_http import PagosHttp
from adaptador_pagos.domain.cobros.modelos import (
    Cobro,
    CobroNoEncontrado,
    EstadoCobro,
    RespuestaInvalida,
    SolicitudCobro,
)
from pydantic import SecretStr

pytestmark = pytest.mark.anyio

BASE = "http://sim/pagos"
CLAVE = UUID("7d3f6a2e-0c1b-4b8e-9f00-000000000a01")
PAGADOR = UUID("00000000-0000-4000-8000-000000000001")
API_KEY = "clave-sintetica-pagos"
SOLICITUD = SolicitudCobro(
    clave_recaudo=CLAVE, monto=Decimal("64638.00"), moneda="COP", pagador_ref=PAGADOR
)
RUTAS = ["cobro", "consulta"]


def _respuesta(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "referencia": str(CLAVE),
        "estado": "APROBADO",
        "transaccionId": "TX-SINT-C01",
        "motivo": None,
    }
    cuerpo.update(cambios)
    return cuerpo


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


def _proveedor() -> PagosHttp:
    return PagosHttp(httpx.AsyncClient(), BASE, SecretStr(API_KEY))


async def _llamar(ruta: str) -> Cobro:
    if ruta == "cobro":
        return await _proveedor().cobrar(SOLICITUD, 5.0)
    return await _proveedor().consultar(CLAVE, PAGADOR, 5.0)


async def test_cobro_envia_encabezados_y_cuerpo_exactos(mock: respx.MockRouter) -> None:
    ruta = mock.post("/v1/cobros").respond(200, json=_respuesta())
    cobro = await _proveedor().cobrar(SOLICITUD, 5.0)
    assert cobro == Cobro(
        clave_recaudo=CLAVE, estado=EstadoCobro.APROBADO, transaccion_id="TX-SINT-C01", motivo=None
    )
    request = ruta.calls.last.request
    assert request.url == f"{BASE}/v1/cobros"
    assert request.headers["Idempotency-Key"] == str(CLAVE)
    assert request.headers["X-Api-Key"] == API_KEY
    assert json.loads(request.content) == {
        "referencia": str(CLAVE),
        "monto": "64638.00",
        "moneda": "COP",
        "pagador": str(PAGADOR),
    }
    assert request.extensions["timeout"]["read"] == 5.0


async def test_cobro_rechazado_con_motivo(mock: respx.MockRouter) -> None:
    mock.post("/v1/cobros").respond(
        200, json=_respuesta(estado="RECHAZADO", transaccionId=None, motivo="FONDOS_INSUFICIENTES")
    )
    assert await _proveedor().cobrar(SOLICITUD, 5.0) == Cobro(
        clave_recaudo=CLAVE,
        estado=EstadoCobro.RECHAZADO,
        transaccion_id=None,
        motivo="FONDOS_INSUFICIENTES",
    )


async def test_consulta_envia_referencia_pagador_y_api_key(mock: respx.MockRouter) -> None:
    ruta = mock.get("/v1/cobros").respond(200, json=_respuesta())
    cobro = await _proveedor().consultar(CLAVE, PAGADOR, 5.0)
    assert cobro.estado == EstadoCobro.APROBADO
    assert cobro.transaccion_id == "TX-SINT-C01"
    request = ruta.calls.last.request
    assert request.url.path == "/pagos/v1/cobros"
    assert dict(request.url.params) == {"referencia": str(CLAVE), "pagador": str(PAGADOR)}
    assert request.headers["X-Api-Key"] == API_KEY
    assert "Idempotency-Key" not in request.headers
    assert request.content == b""
    assert request.extensions["timeout"]["read"] == 5.0


async def test_consulta_rechazada(mock: respx.MockRouter) -> None:
    mock.get("/v1/cobros").respond(
        200, json=_respuesta(estado="RECHAZADO", transaccionId=None, motivo="FONDOS_INSUFICIENTES")
    )
    cobro = await _proveedor().consultar(CLAVE, PAGADOR, 5.0)
    assert (cobro.estado, cobro.motivo) == (EstadoCobro.RECHAZADO, "FONDOS_INSUFICIENTES")


async def test_la_referencia_se_compara_como_uuid(mock: respx.MockRouter) -> None:
    mock.post("/v1/cobros").respond(200, json=_respuesta(referencia=str(CLAVE).upper()))
    assert (await _proveedor().cobrar(SOLICITUD, 5.0)).clave_recaudo == CLAVE


# --- Fallos: el cliente deja los de transporte a la protección y traduce los de forma ---------
@pytest.mark.parametrize("ruta", RUTAS)
@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("lento"), httpx.ConnectTimeout("lento"), httpx.ConnectError("caído")],
)
async def test_timeout_y_conexion_se_propagan(
    mock: respx.MockRouter, ruta: str, error: Exception
) -> None:
    mock.route(path="/v1/cobros").mock(side_effect=error)
    with pytest.raises(type(error)):
        await _llamar(ruta)


@pytest.mark.parametrize("ruta", RUTAS)
@pytest.mark.parametrize("estado", [500, 502, 503, 504])
async def test_5xx_lanza_http_status_error(mock: respx.MockRouter, ruta: str, estado: int) -> None:
    mock.route(path="/v1/cobros").respond(estado, json={"error": "unavailable"})
    with pytest.raises(httpx.HTTPStatusError):
        await _llamar(ruta)


@pytest.mark.parametrize("ruta", RUTAS)
@pytest.mark.parametrize("texto", ['{"estado": ', "<html>", ""])
async def test_cuerpo_malformado(mock: respx.MockRouter, ruta: str, texto: str) -> None:
    mock.route(path="/v1/cobros").respond(200, text=texto)
    with pytest.raises(RespuestaInvalida) as error:
        await _llamar(ruta)
    assert error.value.motivo == "JSON"


FUERA_DE_FORMA = [
    pytest.param([], id="lista"),
    pytest.param("APROBADO", id="texto"),
    pytest.param(_respuesta(estado="PENDIENTE"), id="estado-desconocido"),
    pytest.param({k: v for k, v in _respuesta().items() if k != "estado"}, id="sin-estado"),
    pytest.param(_respuesta(estado=None), id="estado-nulo"),
    pytest.param(_respuesta(transaccionId=None), id="aprobado-sin-transaccion"),
    pytest.param(
        {k: v for k, v in _respuesta().items() if k != "transaccionId"},
        id="aprobado-sin-clave-transaccion",
    ),
    pytest.param(_respuesta(transaccionId=""), id="transaccion-vacia"),
    pytest.param(_respuesta(transaccionId=123), id="transaccion-numero"),
    pytest.param(_respuesta(motivo=7), id="motivo-numero"),
    pytest.param(
        _respuesta(referencia="00000000-0000-4000-8000-0000000000ff"), id="otra-referencia"
    ),
    pytest.param(_respuesta(referencia="no-uuid"), id="referencia-no-uuid"),
    pytest.param(_respuesta(referencia=1), id="referencia-numero"),
    pytest.param({k: v for k, v in _respuesta().items() if k != "referencia"}, id="sin-referencia"),
]


@pytest.mark.parametrize("ruta", RUTAS)
@pytest.mark.parametrize("cuerpo", FUERA_DE_FORMA)
async def test_cuerpo_fuera_de_forma(mock: respx.MockRouter, ruta: str, cuerpo: Any) -> None:
    mock.route(path="/v1/cobros").respond(200, json=cuerpo)
    with pytest.raises(RespuestaInvalida) as error:
        await _llamar(ruta)
    assert error.value.motivo == "JSON"


@pytest.mark.parametrize("ruta", RUTAS)
async def test_404_referencia_no_reconocida_es_respuesta_invalida(
    mock: respx.MockRouter, ruta: str
) -> None:
    mock.route(path="/v1/cobros").respond(404, json={"error": "referencia_no_reconocida"})
    with pytest.raises(RespuestaInvalida) as error:
        await _llamar(ruta)
    assert error.value.motivo == "HTTP_404"


async def test_404_cobro_no_encontrado_en_la_consulta(mock: respx.MockRouter) -> None:
    mock.get("/v1/cobros").respond(404, json={"error": "cobro_no_encontrado"})
    with pytest.raises(CobroNoEncontrado):
        await _proveedor().consultar(CLAVE, PAGADOR, 5.0)


async def test_404_cobro_no_encontrado_en_el_cobro_es_respuesta_invalida(
    mock: respx.MockRouter,
) -> None:
    """Solo la consulta promete `cobro_no_encontrado`: en el cobro no es una respuesta válida."""
    mock.post("/v1/cobros").respond(404, json={"error": "cobro_no_encontrado"})
    with pytest.raises(RespuestaInvalida):
        await _proveedor().cobrar(SOLICITUD, 5.0)


@pytest.mark.parametrize(
    "respuesta",
    [
        pytest.param(httpx.Response(404, text="<html>"), id="sin-json"),
        pytest.param(httpx.Response(404, json=["cobro_no_encontrado"]), id="lista"),
        pytest.param(httpx.Response(404), id="vacio"),
    ],
)
async def test_404_sin_el_codigo_esperado_es_respuesta_invalida(
    mock: respx.MockRouter, respuesta: httpx.Response
) -> None:
    mock.get("/v1/cobros").mock(return_value=respuesta)
    with pytest.raises(RespuestaInvalida):
        await _proveedor().consultar(CLAVE, PAGADOR, 5.0)


@pytest.mark.parametrize("ruta", RUTAS)
@pytest.mark.parametrize("estado", [201, 204, 302, 400, 401, 409, 422])
async def test_otro_estado_es_respuesta_invalida(
    mock: respx.MockRouter, ruta: str, estado: int
) -> None:
    mock.route(path="/v1/cobros").respond(estado, json=_respuesta())
    with pytest.raises(RespuestaInvalida) as error:
        await _llamar(ruta)
    assert error.value.motivo == f"HTTP_{estado}"
