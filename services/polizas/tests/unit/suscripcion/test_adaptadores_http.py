"""CotizacionHttp y CatalogoHttp con un transporte simulado (sin red): mapeo de respuestas,
falla cerrada, propagación del actor y caché TTL de las reglas (DS2-18)."""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from polizas.adapters.outbound.catalogo_http import CatalogoHttp
from polizas.adapters.outbound.cotizacion_http import CotizacionHttp
from polizas.domain.suscripcion.errores import (
    CotizacionNoEncontrada,
    DependenciaNoDisponible,
    ReglasNoVigentes,
)
from polizas.domain.suscripcion.modelos import Actor, RolActor

pytestmark = pytest.mark.anyio
ASESOR = Actor("sub-asesor-1", RolActor.ASESOR)
Manejador = Callable[[httpx.Request], httpx.Response]


def _cliente(manejador: Manejador) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(manejador))


def _respuesta_reglas(sinteticos: Any) -> dict[str, Any]:
    respuesta: dict[str, Any] = sinteticos.respuesta_reglas()
    return respuesta


# --- cotizacion -----------------------------------------------------------------------------


async def test_insumos_con_el_actor_propagado(sinteticos: Any) -> None:
    pedidos: list[httpx.Request] = []

    def responder(pedido: httpx.Request) -> httpx.Response:
        pedidos.append(pedido)
        return httpx.Response(200, json=sinteticos.caso("S05")["insumos"])

    cotizacion = UUID(sinteticos.cotizacion_id("S05"))
    async with _cliente(responder) as cliente:
        insumos = await CotizacionHttp(cliente, "http://cotizacion/", 600).insumos(
            cotizacion, ASESOR
        )
    assert insumos == sinteticos.insumos("S05")
    [pedido] = pedidos
    assert pedido.url.path == f"/v1/cotizaciones/{cotizacion}/insumos-suscripcion"
    assert (pedido.headers["X-Actor-Id"], pedido.headers["X-Actor-Rol"]) == (
        "sub-asesor-1",
        "ASESOR",
    )


@pytest.mark.parametrize(
    ("respuesta", "error"),
    [
        (httpx.Response(404, json={"codigo": "COTIZACION_NO_ENCONTRADA"}), CotizacionNoEncontrada),
        (httpx.Response(404, json={"codigo": "NO_ENCONTRADO"}), DependenciaNoDisponible),
        (httpx.Response(404, text="no json"), DependenciaNoDisponible),
        (httpx.Response(404, json=["lista"]), DependenciaNoDisponible),
        (
            httpx.Response(503, json={"codigo": "DEPENDENCIA_NO_DISPONIBLE"}),
            DependenciaNoDisponible,
        ),
        (httpx.Response(200, json={"cotizacionId": "x"}), DependenciaNoDisponible),
        (httpx.Response(200, text="{"), DependenciaNoDisponible),
    ],
)
async def test_insumos_respuestas_no_validas(
    respuesta: httpx.Response, error: type[Exception], sinteticos: Any
) -> None:
    async with _cliente(lambda _: respuesta) as cliente:
        with pytest.raises(error):
            await CotizacionHttp(cliente, "http://cotizacion", 600).insumos(
                UUID(sinteticos.cotizacion_id("S01")), ASESOR
            )


async def test_insumos_con_la_red_caida(sinteticos: Any) -> None:
    def caida(pedido: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("caída", request=pedido)

    async with _cliente(caida) as cliente:
        with pytest.raises(DependenciaNoDisponible) as error:
            await CotizacionHttp(cliente, "http://cotizacion", 600).insumos(
                UUID(sinteticos.cotizacion_id("S01")), ASESOR
            )
    assert error.value.dependencia == "cotizacion"


# --- catalogo -------------------------------------------------------------------------------


async def test_reglas_vigentes_con_cache_en_memoria(sinteticos: Any, dobles: Any) -> None:
    pedidos: list[str] = []

    def responder(pedido: httpx.Request) -> httpx.Response:
        pedidos.append(pedido.url.path)
        return httpx.Response(200, json=_respuesta_reglas(sinteticos))

    reloj = dobles.reloj
    async with _cliente(responder) as cliente:
        catalogo = CatalogoHttp(cliente, "http://catalogo/", 300, reloj, 60, 3600)
        primera = await catalogo.vigentes("vida-hipotecario")
        segunda = await catalogo.vigentes("vida-hipotecario")
        reloj.instante = reloj.instante + timedelta(seconds=60)
        tercera = await catalogo.vigentes("vida-hipotecario")
        version = await catalogo.por_version("vida-hipotecario", "2026.10.1")
        await catalogo.por_version("vida-hipotecario", "2026.10.1")
    assert primera == segunda == tercera == version == sinteticos.reglas()
    assert pedidos == [
        "/v1/productos/vida-hipotecario/reglas-suscripcion/vigente",
        "/v1/productos/vida-hipotecario/reglas-suscripcion/vigente",
        "/v1/productos/vida-hipotecario/reglas-suscripcion/2026.10.1",
    ]


async def test_sin_cache_con_ttl_cero(sinteticos: Any, dobles: Any) -> None:
    pedidos: list[str] = []

    def responder(pedido: httpx.Request) -> httpx.Response:
        pedidos.append(pedido.url.path)
        return httpx.Response(200, json=_respuesta_reglas(sinteticos))

    async with _cliente(responder) as cliente:
        catalogo = CatalogoHttp(cliente, "http://catalogo", 300, dobles.reloj, 0, 0)
        await catalogo.vigentes("vida-hipotecario")
        await catalogo.vigentes("vida-hipotecario")
    assert len(pedidos) == 2


async def test_pedidos_simultaneos_comparten_la_respuesta(sinteticos: Any, dobles: Any) -> None:
    pedidos: list[str] = []

    async def responder(pedido: httpx.Request) -> httpx.Response:
        pedidos.append(pedido.url.path)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=_respuesta_reglas(sinteticos))

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
        catalogo = CatalogoHttp(cliente, "http://catalogo", 300, dobles.reloj)
        resultados = await asyncio.gather(
            *(catalogo.vigentes("vida-hipotecario") for _ in range(5))
        )
    assert len(pedidos) == 1 and len(set(map(id, resultados))) == 1


def _con_regla(sinteticos: Any, **cambio: Any) -> dict[str, Any]:
    cuerpo = _respuesta_reglas(sinteticos)
    cuerpo["definicion"]["reglas"][0].update(cambio)
    return cuerpo


@pytest.mark.parametrize(
    ("respuesta", "error"),
    [
        (httpx.Response(404, json={"codigo": "REGLAS_NO_VIGENTES"}), ReglasNoVigentes),
        (httpx.Response(404, json={"codigo": "PRODUCTO_NO_ENCONTRADO"}), DependenciaNoDisponible),
        (httpx.Response(404, text="x"), DependenciaNoDisponible),
        (httpx.Response(500, json={}), DependenciaNoDisponible),
        (httpx.Response(200, json={"productoId": "x"}), DependenciaNoDisponible),
    ],
)
async def test_reglas_respuestas_no_validas(
    respuesta: httpx.Response, error: type[Exception], dobles: Any
) -> None:
    async with _cliente(lambda _: respuesta) as cliente:
        with pytest.raises(error):
            await CatalogoHttp(cliente, "http://catalogo", 300, dobles.reloj).vigentes("p")


@pytest.mark.parametrize(
    "cambio",
    [{"campo": "ingreso_mensual"}, {"operador": ">"}, {"motivoCliente": "OTRO"}, {"valor": []}],
)
async def test_una_regla_fuera_de_los_conjuntos_cerrados_es_falla_cerrada(
    cambio: dict[str, Any], sinteticos: Any, dobles: Any
) -> None:
    cuerpo = _con_regla(sinteticos, **cambio)
    async with _cliente(lambda _: httpx.Response(200, json=cuerpo)) as cliente:
        with pytest.raises(DependenciaNoDisponible):
            await CatalogoHttp(cliente, "http://catalogo", 300, dobles.reloj).vigentes("p")


async def test_reglas_con_la_red_caida(dobles: Any) -> None:
    def caida(pedido: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=pedido)

    async with _cliente(caida) as cliente:
        with pytest.raises(DependenciaNoDisponible) as error:
            await CatalogoHttp(cliente, "http://catalogo", 300, dobles.reloj).por_version(
                "p", "2026.10.1"
            )
    assert error.value.dependencia == "catalogo"


# --- catalogo: alternativas (HU-W14) --------------------------------------------------------


async def test_reglas_con_sus_alternativas(sinteticos: Any, dobles: Any) -> None:
    cuerpo = _respuesta_reglas(sinteticos)
    async with _cliente(lambda _: httpx.Response(200, json=cuerpo)) as cliente:
        reglas = await CatalogoHttp(cliente, "http://catalogo", 300, dobles.reloj).por_version(
            "vida-hipotecario", "2026.10.1"
        )
    assert [
        (a.producto_id, a.descriptor.codigo, a.descriptor.valor) for a in reglas.alternativas
    ] == [
        ("vida-senior", "EDAD_MAXIMA", "80"),
        ("proteccion-pagos", "SIN_LIMITE_EDAD", None),
        ("auxilio-funerario", "EDAD_MAXIMA", "85"),
    ]


async def test_una_definicion_sin_alternativas_no_tiene_alternativas(
    sinteticos: Any, dobles: Any
) -> None:
    cuerpo = _respuesta_reglas(sinteticos)
    del cuerpo["definicion"]["alternativas"]
    async with _cliente(lambda _: httpx.Response(200, json=cuerpo)) as cliente:
        reglas = await CatalogoHttp(cliente, "http://catalogo", 300, dobles.reloj).vigentes("p")
    assert reglas.alternativas == ()
    assert reglas.reglas == sinteticos.reglas().reglas


@pytest.mark.parametrize(
    "descriptor", [{"codigo": "OTRO", "valor": None}, {"codigo": "EDAD_MAXIMA", "valor": "80 años"}]
)
async def test_una_alternativa_fuera_del_contrato_es_falla_cerrada(
    descriptor: dict[str, Any], sinteticos: Any, dobles: Any
) -> None:
    cuerpo = _respuesta_reglas(sinteticos)
    cuerpo["definicion"]["alternativas"][0]["descriptor"] = descriptor
    async with _cliente(lambda _: httpx.Response(200, json=cuerpo)) as cliente:
        with pytest.raises(DependenciaNoDisponible):
            await CatalogoHttp(cliente, "http://catalogo", 300, dobles.reloj).vigentes("p")
