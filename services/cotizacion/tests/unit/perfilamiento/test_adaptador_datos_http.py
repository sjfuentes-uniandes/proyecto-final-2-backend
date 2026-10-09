"""UT-W10-30 a 32: adaptador hacia adaptador-datos (03 §B.3 paso 5)."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from cotizacion.adapters.outbound.adaptador_datos_http import AdaptadorDatosHttp
from cotizacion.domain.perfilamiento.modelos import (
    PROPOSITO_PERFILAMIENTO,
    ContextoConsulta,
    EstadoCampo,
    EstadoFuente,
    Fuente,
    SolicitudFuente,
    ValorCampo,
)

pytestmark = pytest.mark.anyio

CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
CONSENTIMIENTO = UUID("00000000-0000-4000-9000-000000000001")
CONTEXTO = ContextoConsulta(CLIENTE, CONSENTIMIENTO, PROPOSITO_PERFILAMIENTO, 290)
PRODUCTOS = SolicitudFuente(
    Fuente.FA_PRODUCTOS_VIGENTES,
    ("antiguedad_productos_anios", "cuota_mensual_obligaciones", "entidades_con_deuda"),
)
OK = {
    "fuente": "FA_PRODUCTOS_VIGENTES",
    "tipo": "FINANZAS_ABIERTAS",
    "proveedor": "SIM-OPEN-FINANCE",
    "versionFuente": "3.1",
    "confianza": 0.95,
    "consultadaEn": "2026-10-15T15:00:00.120000Z",
    "campos": {
        "antiguedad_productos_anios": {"estado": "DISPONIBLE", "valor": "7"},
        "cuota_mensual_obligaciones": {"estado": "DISPONIBLE", "valor": "1800000.00"},
        "entidades_con_deuda": {"estado": "NO_DISPONIBLE", "valor": None},
    },
    "camposDescartados": 3,
}


class RelojFijo:
    def ahora(self) -> datetime:
        return datetime(2026, 10, 15, 15, tzinfo=UTC)


def _adaptador(
    manejador: Callable[[httpx.Request], httpx.Response],
    recibidas: list[httpx.Request] | None = None,
) -> AdaptadorDatosHttp:
    def registrar(request: httpx.Request) -> httpx.Response:
        if recibidas is not None:
            recibidas.append(request)
        return manejador(request)

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(registrar))
    return AdaptadorDatosHttp(cliente, "http://adaptador-datos:8080/", RelojFijo())


def _lanzar(error: BaseException) -> Callable[[httpx.Request], httpx.Response]:
    def manejador(_: httpx.Request) -> httpx.Response:
        raise error

    return manejador


@pytest.mark.parametrize(
    ("manejador", "estado"),
    [
        (lambda _: httpx.Response(200, json=OK), EstadoFuente.CONSULTADA),
        (
            lambda _: httpx.Response(504, json={"codigo": "TIEMPO_AGOTADO"}),
            EstadoFuente.TIEMPO_AGOTADO,
        ),
        (
            lambda _: httpx.Response(503, json={"codigo": "FUENTE_NO_DISPONIBLE"}),
            EstadoFuente.NO_DISPONIBLE,
        ),
        (
            lambda _: httpx.Response(502, json={"codigo": "RESPUESTA_INVALIDA"}),
            EstadoFuente.RESPUESTA_INVALIDA,
        ),
        (
            lambda _: httpx.Response(422, json={"codigo": "CAMPOS_NO_SOPORTADOS"}),
            EstadoFuente.NO_DISPONIBLE,
        ),
        (lambda _: httpx.Response(404), EstadoFuente.NO_DISPONIBLE),
        (lambda _: httpx.Response(500), EstadoFuente.NO_DISPONIBLE),
        (_lanzar(httpx.ReadTimeout("lento")), EstadoFuente.TIEMPO_AGOTADO),
        (_lanzar(httpx.ConnectTimeout("lento")), EstadoFuente.TIEMPO_AGOTADO),
        (_lanzar(httpx.ConnectError("caído")), EstadoFuente.NO_DISPONIBLE),
        (_lanzar(RuntimeError("inesperado")), EstadoFuente.NO_DISPONIBLE),
        (lambda _: httpx.Response(200, text="<html>"), EstadoFuente.RESPUESTA_INVALIDA),
        (
            lambda _: httpx.Response(200, json={**OK, "confianza": "alta"}),
            EstadoFuente.RESPUESTA_INVALIDA,
        ),
    ],
)
async def test_ut_w10_30_cada_respuesta_produce_su_estado(
    manejador: Callable[[httpx.Request], httpx.Response], estado: EstadoFuente
) -> None:
    resultado = await _adaptador(manejador).consultar(PRODUCTOS, CONTEXTO)
    assert resultado.estado is estado
    assert resultado.fuente is Fuente.FA_PRODUCTOS_VIGENTES
    assert resultado.campos_solicitados == PRODUCTOS.campos
    assert resultado.duracion_ms is not None and resultado.duracion_ms >= 0
    if estado is not EstadoFuente.CONSULTADA:
        assert resultado.campos == {}
        assert (resultado.version_fuente, resultado.confianza, resultado.consultada_en) == (
            None,
            None,
            None,
        )
        assert resultado.proveedor is None


async def test_la_cancelacion_se_propaga() -> None:
    with pytest.raises(asyncio.CancelledError):
        await _adaptador(_lanzar(asyncio.CancelledError())).consultar(PRODUCTOS, CONTEXTO)


async def test_ut_w10_31_campos_decimales_y_linaje() -> None:
    resultado = await _adaptador(lambda _: httpx.Response(200, json=OK)).consultar(
        PRODUCTOS, CONTEXTO
    )
    assert resultado.campos == {
        "antiguedad_productos_anios": ValorCampo(EstadoCampo.DISPONIBLE, Decimal(7)),
        "cuota_mensual_obligaciones": ValorCampo(EstadoCampo.DISPONIBLE, Decimal("1800000.00")),
        "entidades_con_deuda": ValorCampo(EstadoCampo.NO_DISPONIBLE, None),
    }
    assert all(
        isinstance(v.valor, Decimal) for v in resultado.campos.values() if v.valor is not None
    )
    assert resultado.campos_solicitados == PRODUCTOS.campos
    assert resultado.confianza == Decimal("0.95")
    assert resultado.version_fuente == "3.1"
    assert resultado.proveedor == "SIM-OPEN-FINANCE"
    assert resultado.consultada_en == datetime(2026, 10, 15, 15, 0, 0, 120000, tzinfo=UTC)


async def test_un_campo_no_solicitado_nunca_entra_al_dominio() -> None:
    cuerpo: dict[str, Any] = json.loads(json.dumps(OK))
    cuerpo["campos"]["ingreso_mensual_estimado"] = {"estado": "DISPONIBLE", "valor": "6500000.00"}
    resultado = await _adaptador(lambda _: httpx.Response(200, json=cuerpo)).consultar(
        PRODUCTOS, CONTEXTO
    )
    assert "ingreso_mensual_estimado" not in resultado.campos


async def test_ut_w10_32_cuerpo_encabezado_y_timeout() -> None:
    recibidas: list[httpx.Request] = []
    await _adaptador(lambda _: httpx.Response(200, json=OK), recibidas).consultar(
        PRODUCTOS, CONTEXTO
    )
    (request,) = recibidas
    assert str(request.url) == "http://adaptador-datos:8080/v1/consultas"
    assert json.loads(request.content) == {
        "fuente": "FA_PRODUCTOS_VIGENTES",
        "clienteId": str(CLIENTE),
        "consentimientoId": str(CONSENTIMIENTO),
        "proposito": "PERFILAMIENTO_PRECIO",
        "camposAutorizados": list(PRODUCTOS.campos),
    }
    assert request.headers["X-Deadline-Ms"] == "290"
    assert request.extensions["timeout"]["read"] == pytest.approx(0.34)


async def test_ut_w10_32_sin_consentimiento_se_envia_nulo() -> None:
    recibidas: list[httpx.Request] = []
    contexto = ContextoConsulta(CLIENTE, None, PROPOSITO_PERFILAMIENTO, 290)
    respuesta = httpx.Response(422, json={"codigo": "CONSENTIMIENTO_REQUERIDO"})
    resultado = await _adaptador(lambda _: respuesta, recibidas).consultar(PRODUCTOS, contexto)
    assert json.loads(recibidas[0].content)["consentimientoId"] is None
    assert resultado.estado is EstadoFuente.NO_DISPONIBLE
