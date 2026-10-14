"""Adaptador hacia catalogo con caché del modelo vigente (UT-W11-13 a 16)."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from cotizacion.adapters.outbound.catalogo_http import DEPENDENCIA_MODELO, CatalogoHttp
from cotizacion.domain.perfilamiento.errores import DependenciaNoDisponible, ModeloNoDisponible
from cotizacion.domain.perfilamiento.modelo_riesgo import Expresion
from cotizacion.domain.perfilamiento.modelos import Fuente, NivelRiesgo

pytestmark = pytest.mark.anyio

BASE = "http://catalogo:8080/"
EN = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)


class RelojFalso:
    def __init__(self) -> None:
        self.instante = EN

    def ahora(self) -> datetime:
        return self.instante


def _catalogo(
    manejador: Callable[[httpx.Request], httpx.Response],
    recibidas: list[httpx.Request],
    ttl_s: int = 60,
    reloj: RelojFalso | None = None,
) -> CatalogoHttp:
    def registrar(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return manejador(request)

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(registrar))
    return CatalogoHttp(cliente, BASE, ttl_s, 150, reloj or RelojFalso())


async def test_convierte_la_definicion_al_dominio(respuesta_candidata: dict[str, Any]) -> None:
    recibidas: list[httpx.Request] = []
    catalogo = _catalogo(lambda _: httpx.Response(200, json=respuesta_candidata), recibidas)
    modelo = await catalogo.vigente("vida-hipotecario", EN)
    assert (modelo.version, modelo.hash, modelo.producto_id) == (
        "2026.10.1",
        "sha256:2026.10.1",
        "vida-hipotecario",
    )
    assert list(modelo.fuentes) == list(Fuente)
    assert modelo.fuentes[Fuente.FA_HISTORIAL_PAGOS_12M] == ("moras_12m",)
    assert modelo.obligatorios == {
        "edad",
        "plazo_credito_meses",
        "moras_12m",
        "cuota_mensual_obligaciones",
    }
    # Los literales numéricos pasan a Decimal; los nombres quedan como variables.
    assert modelo.derivados["edad_fin_credito"] == Expresion(
        "add", ("edad", Expresion("div", ("plazo_credito_meses", Decimal(12))))
    )
    assert [f.peso for f in modelo.factores] == [
        Decimal(p) for p in ("0.30", "0.20", "0.18", "0.15", "0.10", "0.07")
    ]
    assert modelo.ajustes[0].nivel is NivelRiesgo.ALTO
    assert modelo.niveles[-1].desde == Decimal(0)
    assert modelo.penalizacion_no_obtenida == Decimal("0.15")
    (solicitud,) = recibidas
    assert solicitud.url.path == "/v1/modelos-riesgo/vigente"
    assert dict(solicitud.url.params) == {
        "productoId": "vida-hipotecario",
        "en": "2026-10-15T15:00:00+00:00",
    }


async def test_ut_w11_13_acierto_de_cache_no_vuelve_a_llamar(
    respuesta_candidata: dict[str, Any],
) -> None:
    recibidas: list[httpx.Request] = []
    catalogo = _catalogo(lambda _: httpx.Response(200, json=respuesta_candidata), recibidas)
    primero = await catalogo.vigente("vida-hipotecario", EN)
    assert await catalogo.vigente("vida-hipotecario", EN) is primero
    assert len(recibidas) == 1


async def test_ut_w11_14_la_cache_vence_con_el_reloj(respuesta_candidata: dict[str, Any]) -> None:
    recibidas: list[httpx.Request] = []
    reloj = RelojFalso()
    catalogo = _catalogo(
        lambda _: httpx.Response(200, json=respuesta_candidata), recibidas, reloj=reloj
    )
    await catalogo.vigente("vida-hipotecario", EN)
    reloj.instante = EN + timedelta(seconds=59, microseconds=999999)
    await catalogo.vigente("vida-hipotecario", EN)
    assert len(recibidas) == 1
    reloj.instante = EN + timedelta(seconds=60)
    await catalogo.vigente("vida-hipotecario", EN)
    assert len(recibidas) == 2


async def test_sin_ttl_la_vigente_no_se_guarda(respuesta_candidata: dict[str, Any]) -> None:
    recibidas: list[httpx.Request] = []
    catalogo = _catalogo(
        lambda _: httpx.Response(200, json=respuesta_candidata), recibidas, ttl_s=0
    )
    await catalogo.vigente("vida-hipotecario", EN)
    await catalogo.vigente("vida-hipotecario", EN)
    assert len(recibidas) == 2


async def test_una_version_cerrada_no_vence(respuesta_candidata: dict[str, Any]) -> None:
    recibidas: list[httpx.Request] = []
    reloj = RelojFalso()
    catalogo = _catalogo(
        lambda _: httpx.Response(200, json=respuesta_candidata), recibidas, ttl_s=0, reloj=reloj
    )
    modelo = await catalogo.por_version("2026.10.1")
    reloj.instante = EN + timedelta(days=365)
    assert await catalogo.por_version("2026.10.1") is modelo
    assert [r.url.path for r in recibidas] == ["/v1/modelos-riesgo/2026.10.1"]
    assert recibidas[0].url.query == b""


async def test_ut_w11_15_veinte_llamadas_en_frio_hacen_una_solicitud(
    respuesta_candidata: dict[str, Any],
) -> None:
    recibidas: list[httpx.Request] = []
    cuerpo = json.dumps(respuesta_candidata).encode()

    class Lento(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            recibidas.append(request)
            await asyncio.sleep(0.02)
            return httpx.Response(200, content=cuerpo)

    catalogo = CatalogoHttp(httpx.AsyncClient(transport=Lento()), BASE, 60, 150, RelojFalso())
    modelos = await asyncio.gather(*(catalogo.vigente("vida-hipotecario", EN) for _ in range(20)))
    assert len(recibidas) == 1
    assert all(m is modelos[0] for m in modelos)


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("lento", request=request)


def _caido(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("caído", request=request)


@pytest.mark.parametrize(
    "manejador",
    [
        lambda _: httpx.Response(503),
        lambda _: httpx.Response(500, json={"codigo": "ERROR_INTERNO"}),
        lambda _: httpx.Response(422, json={"codigo": "SOLICITUD_INVALIDA"}),
        lambda _: httpx.Response(200, text="no es json"),
        lambda _: httpx.Response(200, json={"version": "2026.10.1"}),
        _timeout,
        _caido,
    ],
    ids=["503", "500", "422", "json-invalido", "fuera-del-esquema", "timeout", "conexion"],
)
async def test_ut_w11_16_fallas_tecnicas_son_dependencia_no_disponible(
    manejador: Callable[[httpx.Request], httpx.Response],
) -> None:
    catalogo = _catalogo(manejador, [])
    with pytest.raises(DependenciaNoDisponible) as error:
        await catalogo.vigente("vida-hipotecario", EN)
    assert error.value.dependencia == DEPENDENCIA_MODELO == "catalogo.modelo"


async def test_ut_w11_16_fuente_desconocida_viola_el_contrato(
    respuesta_candidata: dict[str, Any],
) -> None:
    fuentes = respuesta_candidata["definicion"]["fuentes"]
    fuentes["FA_SALDOS"] = fuentes.pop("FA_INGRESOS_AGREGADOS")
    catalogo = _catalogo(lambda _: httpx.Response(200, json=respuesta_candidata), [])
    with pytest.raises(DependenciaNoDisponible):
        await catalogo.por_version("2026.10.1")


async def test_ut_w11_16_404_es_modelo_no_disponible() -> None:
    cuerpo = {
        "codigo": "MODELO_NO_ENCONTRADO",
        "mensaje": "x",
        "correlationId": None,
        "detalles": {},
    }
    catalogo = _catalogo(lambda _: httpx.Response(404, json=cuerpo), [])
    with pytest.raises(ModeloNoDisponible):
        await catalogo.vigente("vida-hipotecario", EN)


async def test_un_error_no_queda_en_cache(respuesta_candidata: dict[str, Any]) -> None:
    respuestas = iter([httpx.Response(503), httpx.Response(200, json=respuesta_candidata)])
    recibidas: list[httpx.Request] = []
    catalogo = _catalogo(lambda _: next(respuestas), recibidas)
    with pytest.raises(DependenciaNoDisponible):
        await catalogo.vigente("vida-hipotecario", EN)
    assert (await catalogo.vigente("vida-hipotecario", EN)).version == "2026.10.1"
    assert len(recibidas) == 2
