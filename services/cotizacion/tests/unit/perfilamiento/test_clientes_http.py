"""UT-W10-26 a 29: adaptador hacia clientes (contrato propuesto, 01 §3.4.1)."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from cotizacion.adapters.outbound.clientes_http import ClientesHttp
from cotizacion.domain.perfilamiento.errores import ClienteNoEncontrado, DependenciaNoDisponible
from cotizacion.domain.perfilamiento.modelos import (
    PROPOSITO_PERFILAMIENTO,
    DecisionConsentimiento,
    EstadoIdentidad,
    Fuente,
)
from solventa_common import resiliencia

pytestmark = pytest.mark.anyio

CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
BASE = "http://simulador-aliados:8080/clientes-stub/"
TODAS = list(Fuente)


def _clientes(
    manejador: Callable[[httpx.Request], httpx.Response],
    recibidas: list[httpx.Request] | None = None,
) -> ClientesHttp:
    def registrar(request: httpx.Request) -> httpx.Response:
        if recibidas is not None:
            recibidas.append(request)
        return manejador(request)

    return ClientesHttp(httpx.AsyncClient(transport=httpx.MockTransport(registrar)), BASE, 150)


def _identidad(estado: str, verificado_en: str | None = "2026-10-01T00:00:00Z") -> dict[str, Any]:
    return {"clienteId": str(CLIENTE), "estado": estado, "verificadoEn": verificado_en}


def _verificacion(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "decision": "PERMITIDO",
        "consentimientoId": "00000000-0000-4000-9000-000000000001",
        "vigenteHasta": "2027-10-01T00:00:00Z",
        "alcancesPermitidos": [str(f) for f in TODAS],
        "alcancesRechazados": [],
        "causa": None,
    }
    cuerpo.update(cambios)
    return cuerpo


@pytest.mark.parametrize("estado", list(EstadoIdentidad))
async def test_ut_w10_26_cada_estado_de_identidad(estado: EstadoIdentidad) -> None:
    recibidas: list[httpx.Request] = []
    clientes = _clientes(lambda _: httpx.Response(200, json=_identidad(estado, None)), recibidas)
    assert await clientes.estado(CLIENTE) is estado
    (request,) = recibidas
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}v1/clientes/{CLIENTE}/identidad"
    assert request.content == b""
    assert request.extensions["timeout"]["read"] == 0.15


async def test_ut_w10_26_consentimiento_permitido_con_vigencia_utc() -> None:
    recibidas: list[httpx.Request] = []
    cuerpo = _verificacion(vigenteHasta="2027-10-01T05:00:00+05:00")
    clientes = _clientes(lambda _: httpx.Response(200, json=cuerpo), recibidas)
    verificacion = await clientes.verificar(CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS)
    assert verificacion.decision is DecisionConsentimiento.PERMITIDO
    assert verificacion.consentimiento_id == UUID("00000000-0000-4000-9000-000000000001")
    assert verificacion.vigente_hasta == datetime(2027, 10, 1, tzinfo=UTC)
    assert verificacion.vigente_hasta.tzinfo is UTC
    assert verificacion.alcances_permitidos == frozenset(TODAS)
    (request,) = recibidas
    assert request.method == "POST"
    assert (
        request.url.path == f"/clientes-stub/v1/clientes/{CLIENTE}/consentimientos/verificaciones"
    )
    assert json.loads(request.content) == {
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": [str(f) for f in TODAS],
    }


async def test_ut_w10_26_vigencia_sin_zona_se_interpreta_utc() -> None:
    cuerpo = _verificacion(vigenteHasta="2027-10-01T00:00:00")
    verificacion = await _clientes(lambda _: httpx.Response(200, json=cuerpo)).verificar(
        CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS
    )
    assert verificacion.vigente_hasta == datetime(2027, 10, 1, tzinfo=UTC)


async def test_ut_w10_27_consentimiento_parcial_y_rechazado() -> None:
    parcial = _verificacion(
        decision="PERMITIDO_PARCIAL",
        alcancesPermitidos=[
            "FA_PRODUCTOS_VIGENTES",
            "FA_HISTORIAL_PAGOS_12M",
            "DA_REGISTROS_PUBLICOS",
        ],
        alcancesRechazados=[{"alcance": "FA_INGRESOS_AGREGADOS", "causa": "ALCANCE_NO_AUTORIZADO"}],
    )
    verificacion = await _clientes(lambda _: httpx.Response(200, json=parcial)).verificar(
        CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS
    )
    assert verificacion.decision is DecisionConsentimiento.PERMITIDO_PARCIAL
    assert Fuente.FA_INGRESOS_AGREGADOS not in verificacion.alcances_permitidos
    rechazado = _verificacion(
        decision="RECHAZADO",
        consentimientoId=None,
        vigenteHasta=None,
        alcancesPermitidos=[],
        causa="REVOCADO",
    )
    verificacion = await _clientes(lambda _: httpx.Response(200, json=rechazado)).verificar(
        CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS
    )
    assert verificacion.decision is DecisionConsentimiento.RECHAZADO
    assert (verificacion.consentimiento_id, verificacion.vigente_hasta) == (None, None)
    assert verificacion.alcances_permitidos == frozenset()
    assert verificacion.causa == "REVOCADO"


async def test_ut_w10_27_404_cliente_no_encontrado() -> None:
    no_encontrado = {
        "codigo": "CLIENTE_NO_ENCONTRADO",
        "mensaje": "x",
        "correlationId": None,
        "detalles": {},
    }
    clientes = _clientes(lambda _: httpx.Response(404, json=no_encontrado))
    with pytest.raises(ClienteNoEncontrado):
        await clientes.estado(CLIENTE)
    with pytest.raises(ClienteNoEncontrado):
        await clientes.verificar(CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS)


def _lanzar(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def manejador(_: httpx.Request) -> httpx.Response:
        raise error

    return manejador


@pytest.mark.parametrize(
    "manejador",
    [
        _lanzar(httpx.ReadTimeout("lento")),
        _lanzar(httpx.ConnectError("caído")),
        lambda _: httpx.Response(503),
        lambda _: httpx.Response(500),
        lambda _: httpx.Response(400),
        lambda _: httpx.Response(200, text="<html>"),
    ],
)
async def test_ut_w10_28_timeout_y_fallas_son_dependencia_del_dominio(
    manejador: Callable[[httpx.Request], httpx.Response],
) -> None:
    clientes = _clientes(manejador)
    with pytest.raises(DependenciaNoDisponible) as identidad:
        await clientes.estado(CLIENTE)
    assert identidad.value.dependencia == "clientes.identidad"
    assert not isinstance(identidad.value, resiliencia.DependenciaNoDisponible)
    with pytest.raises(DependenciaNoDisponible) as consentimiento:
        await clientes.verificar(CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS)
    assert consentimiento.value.dependencia == "clientes.consentimiento"


@pytest.mark.parametrize(
    "cuerpo",
    [
        _verificacion(alcancesPermitidos=["FA_PRODUCTOS_VIGENTES", "FA_NUEVO_ALCANCE"]),
        _verificacion(decision="QUIZAS"),
        _verificacion(causa="OTRA_CAUSA"),
        {"decision": "PERMITIDO"},
    ],
)
async def test_ut_w10_29_respuesta_fuera_de_contrato_en_consentimiento(
    cuerpo: dict[str, Any],
) -> None:
    with pytest.raises(DependenciaNoDisponible) as error:
        await _clientes(lambda _: httpx.Response(200, json=cuerpo)).verificar(
            CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS
        )
    assert error.value.dependencia == "clientes.consentimiento"


async def test_ut_w10_29_estado_desconocido_de_identidad() -> None:
    clientes = _clientes(lambda _: httpx.Response(200, json=_identidad("EN_REVISION")))
    with pytest.raises(DependenciaNoDisponible) as error:
        await clientes.estado(CLIENTE)
    assert error.value.dependencia == "clientes.identidad"
