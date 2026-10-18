"""Adaptador hacia clientes (contrato propuesto de identidad y consentimiento)."""

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
BASE = "http://pasarela:8080/clientes/"
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
async def test_cada_estado_de_identidad(estado: EstadoIdentidad) -> None:
    recibidas: list[httpx.Request] = []
    clientes = _clientes(lambda _: httpx.Response(200, json=_identidad(estado, None)), recibidas)
    assert await clientes.estado(CLIENTE) is estado
    (request,) = recibidas
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}v1/clientes/{CLIENTE}/identidad"
    assert request.content == b""
    assert request.extensions["timeout"]["read"] == 0.15


async def test_consentimiento_permitido_con_vigencia_utc() -> None:
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
    assert request.url.path == f"/clientes/v1/clientes/{CLIENTE}/consentimientos/verificaciones"
    assert json.loads(request.content) == {
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": [str(f) for f in TODAS],
    }


async def test_vigencia_sin_zona_se_interpreta_utc() -> None:
    cuerpo = _verificacion(vigenteHasta="2027-10-01T00:00:00")
    verificacion = await _clientes(lambda _: httpx.Response(200, json=cuerpo)).verificar(
        CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS
    )
    assert verificacion.vigente_hasta == datetime(2027, 10, 1, tzinfo=UTC)


async def test_consentimiento_parcial_y_rechazado() -> None:
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


async def test_404_cliente_no_encontrado() -> None:
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


async def test_404_con_el_codigo_basta_aunque_falten_otros_campos() -> None:
    clientes = _clientes(lambda _: httpx.Response(404, json={"codigo": "CLIENTE_NO_ENCONTRADO"}))
    with pytest.raises(ClienteNoEncontrado):
        await clientes.verificar(CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS)


# Un 404 sin el `codigo` del contrato viene de una ruta, un gateway u otro servicio: no dice
# nada del cliente y falla cerrado como cualquier otro fallo técnico.
NO_ENCONTRADOS_AJENOS: list[httpx.Response] = [
    httpx.Response(404),
    httpx.Response(404, text="Not Found"),
    httpx.Response(404, text="<html><body>404</body></html>"),
    httpx.Response(404, content=b'{"codigo": "CLIENTE_NO_ENCONTRADO"'),
    httpx.Response(404, json={"message": "Not Found"}),
    httpx.Response(404, json={"codigo": "RUTA_NO_ENCONTRADA", "mensaje": "x"}),
    httpx.Response(404, json={"codigo": "cliente_no_encontrado"}),
    httpx.Response(404, json={"codigo": None}),
    httpx.Response(404, json={"codigo": ["CLIENTE_NO_ENCONTRADO"]}),
    httpx.Response(404, json=["CLIENTE_NO_ENCONTRADO"]),
    httpx.Response(404, json=None),
    httpx.Response(404, json="CLIENTE_NO_ENCONTRADO"),
]
IDS_NO_ENCONTRADOS = [
    "vacio",
    "texto",
    "html",
    "json-truncado",
    "codigo-ausente",
    "otro-codigo",
    "codigo-en-minusculas",
    "codigo-nulo",
    "codigo-en-lista",
    "lista",
    "null",
    "texto-json",
]


@pytest.mark.parametrize("respuesta", NO_ENCONTRADOS_AJENOS, ids=IDS_NO_ENCONTRADOS)
async def test_404_ajeno_en_la_verificacion_es_dependencia_no_disponible(
    respuesta: httpx.Response,
) -> None:
    with pytest.raises(DependenciaNoDisponible) as error:
        await _clientes(lambda _: respuesta).verificar(CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS)
    with pytest.raises(DependenciaNoDisponible) as tecnico:
        await _clientes(lambda _: httpx.Response(503)).verificar(
            CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS
        )
    assert type(error.value) is type(tecnico.value)
    assert (error.value.dependencia, str(error.value)) == (
        tecnico.value.dependencia,
        str(tecnico.value),
    )
    assert error.value.dependencia == "clientes.consentimiento"


@pytest.mark.parametrize("respuesta", NO_ENCONTRADOS_AJENOS, ids=IDS_NO_ENCONTRADOS)
async def test_404_ajeno_en_la_identidad_es_dependencia_no_disponible(
    respuesta: httpx.Response,
) -> None:
    """La regla vive en `_enviar`, que comparten las dos rutas."""
    with pytest.raises(DependenciaNoDisponible) as error:
        await _clientes(lambda _: respuesta).estado(CLIENTE)
    assert error.value.dependencia == "clientes.identidad"


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
async def test_timeout_y_fallas_son_dependencia_del_dominio(
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
async def test_respuesta_fuera_de_contrato_en_consentimiento(
    cuerpo: dict[str, Any],
) -> None:
    with pytest.raises(DependenciaNoDisponible) as error:
        await _clientes(lambda _: httpx.Response(200, json=cuerpo)).verificar(
            CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS
        )
    assert error.value.dependencia == "clientes.consentimiento"


async def test_estado_desconocido_de_identidad() -> None:
    clientes = _clientes(lambda _: httpx.Response(200, json=_identidad("EN_REVISION")))
    with pytest.raises(DependenciaNoDisponible) as error:
        await clientes.estado(CLIENTE)
    assert error.value.dependencia == "clientes.identidad"


# --- HU-W31 · la verificación falla cerrada -----------------------------------------------
# Nada de esto lo produce `clientes` según su contrato (por eso no está en el pacto): es lo que
# puede pasar en el camino. Ninguno debe dar una verificación; todos son "no vigente".

FALLOS_DE_VERIFICACION: list[Callable[[httpx.Request], httpx.Response]] = [
    _lanzar(httpx.ConnectTimeout("sin conexión a tiempo")),
    _lanzar(httpx.WriteTimeout("lento al enviar")),
    _lanzar(httpx.PoolTimeout("pool agotado")),
    _lanzar(httpx.RemoteProtocolError("conexión cortada")),
    lambda _: httpx.Response(502),
    lambda _: httpx.Response(504),
    lambda _: httpx.Response(401),
    lambda _: httpx.Response(403),
    lambda _: httpx.Response(409),
    lambda _: httpx.Response(422),
    lambda _: httpx.Response(201, json=_verificacion()),
    lambda _: httpx.Response(204),
    lambda _: httpx.Response(200, content=b""),
    lambda _: httpx.Response(200, json=None),
    lambda _: httpx.Response(200, json=[]),
    lambda _: httpx.Response(200, json="PERMITIDO"),
    lambda _: httpx.Response(200, content=b'{"decision": "PERMITIDO"'),
]
IDS_FALLOS = [
    "connect-timeout",
    "write-timeout",
    "pool-timeout",
    "conexion-cortada",
    "502",
    "504",
    "401",
    "403",
    "409",
    "422",
    "201-con-cuerpo-valido",
    "204",
    "200-vacio",
    "200-null",
    "200-lista",
    "200-texto",
    "200-json-truncado",
]

FUERA_DE_CONTRATO: list[dict[str, Any]] = [
    _verificacion(decision="permitido"),
    _verificacion(decision=None),
    _verificacion(causa=""),
    _verificacion(alcancesPermitidos=None),
    _verificacion(
        decision="PERMITIDO_PARCIAL",
        alcancesRechazados=[{"alcance": "FA_INGRESOS_AGREGADOS", "causa": "OTRA_CAUSA"}],
    ),
    _verificacion(
        decision="PERMITIDO_PARCIAL",
        alcancesRechazados=[{"alcance": "FA_NUEVO_ALCANCE", "causa": "ALCANCE_NO_AUTORIZADO"}],
    ),
    _verificacion(consentimientoId="no-es-un-uuid"),
    _verificacion(vigenteHasta="mañana"),
]
IDS_FUERA = [
    "decision-en-minusculas",
    "decision-nula",
    "causa-vacia",
    "alcances-permitidos-nulos",
    "causa-de-alcance-desconocida",
    "alcance-rechazado-desconocido",
    "consentimiento-no-uuid",
    "vigencia-ilegible",
]


@pytest.mark.parametrize("manejador", FALLOS_DE_VERIFICACION, ids=IDS_FALLOS)
async def test_fallo_tecnico_de_la_verificacion_es_dependencia_no_disponible(
    manejador: Callable[[httpx.Request], httpx.Response],
) -> None:
    with pytest.raises(DependenciaNoDisponible) as error:
        await _clientes(manejador).verificar(CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS)
    assert error.value.dependencia == "clientes.consentimiento"


@pytest.mark.parametrize("cuerpo", FUERA_DE_CONTRATO, ids=IDS_FUERA)
async def test_verificacion_fuera_de_contrato_es_dependencia_no_disponible(
    cuerpo: dict[str, Any],
) -> None:
    with pytest.raises(DependenciaNoDisponible) as error:
        await _clientes(lambda _: httpx.Response(200, json=cuerpo)).verificar(
            CLIENTE, PROPOSITO_PERFILAMIENTO, TODAS
        )
    assert error.value.dependencia == "clientes.consentimiento"


@pytest.mark.parametrize(
    "manejador",
    [
        _lanzar(httpx.ReadTimeout("lento")),
        lambda _: httpx.Response(500),
        lambda _: httpx.Response(503),
        lambda _: httpx.Response(200, text="<html>"),
        lambda _: httpx.Response(200, json=_verificacion(decision="QUIZAS")),
        lambda _: httpx.Response(200, json=_verificacion(causa="OTRA_CAUSA")),
        lambda _: httpx.Response(404, text="Not Found"),
    ],
    ids=[
        "timeout",
        "500",
        "503",
        "cuerpo-no-json",
        "decision-desconocida",
        "causa-desconocida",
        "404-ajeno",
    ],
)
async def test_el_perfilamiento_no_consulta_fuentes_si_la_verificacion_falla(
    escenario: Any,
    nuevo_comando: Any,
    manejador: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Con el adaptador real: el fallo de `clientes` corta en la fase A, antes de las fuentes."""
    escenario.consentimiento = _clientes(manejador)
    with pytest.raises(DependenciaNoDisponible) as error:
        await escenario.caso().ejecutar(nuevo_comando())
    assert error.value.dependencia == "clientes.consentimiento"
    assert escenario.ejecutor.llamadas == []
    assert escenario.repo.perfiles == {}


async def test_el_perfilamiento_sigue_viendo_el_cliente_inexistente_del_contrato(
    escenario: Any, nuevo_comando: Any
) -> None:
    """El 404 con `CLIENTE_NO_ENCONTRADO` sigue siendo negocio (404 al usuario), no falla técnica."""
    no_encontrado = {"codigo": "CLIENTE_NO_ENCONTRADO", "mensaje": "x", "correlationId": None}
    escenario.consentimiento = _clientes(lambda _: httpx.Response(404, json=no_encontrado))
    with pytest.raises(ClienteNoEncontrado):
        await escenario.caso().ejecutar(nuevo_comando())
    assert escenario.ejecutor.llamadas == []
    assert escenario.repo.perfiles == {}
