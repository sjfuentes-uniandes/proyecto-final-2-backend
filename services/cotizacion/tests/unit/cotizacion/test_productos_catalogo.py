"""ProductosCatalogo (HU-W03): HTTP a catalogo, lectura de Valkey (sin escribir nunca) y caché
en memoria cuando no hay CACHE_URL."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from cotizacion.adapters.outbound.productos_catalogo import ProductosCatalogo
from cotizacion.application.cotizacion.puertos import OrigenDato
from cotizacion.domain.cotizacion.errores import ProductoNoEncontrado, ReglasNoVigentes
from cotizacion.domain.perfilamiento.errores import DependenciaNoDisponible

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
SEMILLAS = Path(__file__).resolve().parents[4] / "catalogo/src/catalogo/adapters/outbound/semillas"
CLAVE_PRODUCTO = "catalogo:v1:producto:vida-hipotecario"
CLAVE_REGLAS = "catalogo:v1:reglas-prima:vida-hipotecario:vigente"


def producto_json() -> dict[str, Any]:
    entrada = json.loads((SEMILLAS / "productos.json").read_text(encoding="utf-8"))[0]
    return {k: v for k, v in entrada.items() if k != "reglas"}


def reglas_json(vigente_desde: str = "2026-09-01T00:00:00Z") -> dict[str, Any]:
    definicion = json.loads((SEMILLAS / "reglas_prima_2026_09_1.json").read_text(encoding="utf-8"))
    return {
        "productoId": "vida-hipotecario",
        "version": "2026.09.1",
        "vigenteDesde": vigente_desde,
        "hash": "sha256:reglas",
        "definicion": definicion,
    }


class Catalogo:
    """catalogo simulado: responde según la ruta y cuenta las solicitudes."""

    def __init__(self) -> None:
        self.solicitudes: list[httpx.Request] = []
        self.respuestas: dict[str, httpx.Response] = {}
        self.error: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.solicitudes.append(request)
        if self.error:
            raise self.error
        for sufijo, respuesta in self.respuestas.items():
            if request.url.path.endswith(sufijo):
                return respuesta
        if request.url.path.endswith("/vigente"):
            return httpx.Response(200, json=reglas_json())
        return httpx.Response(200, json=producto_json())


class CacheLectura:
    def __init__(self, valores: dict[str, bytes] | None = None) -> None:
        self.valores = valores or {}
        self.escrituras = 0

    async def leer(self, clave: str) -> bytes | None:
        return self.valores.get(clave)

    async def escribir(self, clave: str, valor: bytes, ttl_s: int) -> None:
        self.escrituras += 1

    async def borrar(self, *claves: str) -> None:
        return None

    async def cerrar(self) -> None:
        return None


class Reloj:
    def __init__(self) -> None:
        self.instante = AHORA

    def ahora(self) -> datetime:
        return self.instante


@pytest.fixture
def catalogo() -> Catalogo:
    return Catalogo()


@pytest.fixture
def reloj() -> Reloj:
    return Reloj()


def adaptador(catalogo: Catalogo, reloj: Reloj, **opciones: Any) -> ProductosCatalogo:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(catalogo))
    return ProductosCatalogo(cliente, "http://catalogo:8080/", 150, reloj, **opciones)


async def test_pide_el_producto_y_las_reglas_con_la_fecha(catalogo: Catalogo, reloj: Reloj) -> None:
    productos = adaptador(catalogo, reloj, ttl_s=0)
    producto = await productos.producto("vida-hipotecario")
    reglas = await productos.reglas_vigentes("vida-hipotecario", AHORA)
    assert (producto.origen, reglas.origen) == (OrigenDato.CATALOGO, OrigenDato.CATALOGO)
    assert producto.valor.moneda == "COP"
    assert reglas.valor.recargos["ENFERMEDADES_GRAVES"] == Decimal("0.18")
    assert str(catalogo.solicitudes[0].url) == "http://catalogo:8080/v1/productos/vida-hipotecario"
    assert catalogo.solicitudes[1].url.params["fecha"] == AHORA.isoformat()


@pytest.mark.parametrize(
    ("respuesta", "error"),
    [
        (httpx.Response(404, json={"codigo": "PRODUCTO_NO_ENCONTRADO"}), ProductoNoEncontrado),
        (httpx.Response(404, json={"codigo": "REGLAS_NO_VIGENTES"}), ReglasNoVigentes),
        (httpx.Response(404, json={"codigo": "NO_ENCONTRADO"}), DependenciaNoDisponible),
        (httpx.Response(404, text="no es json"), DependenciaNoDisponible),
        (httpx.Response(404, json=[]), DependenciaNoDisponible),
        (httpx.Response(500), DependenciaNoDisponible),
        (httpx.Response(200, json={"productoId": "x"}), DependenciaNoDisponible),
    ],
    ids=[
        "producto",
        "reglas",
        "404 ajeno",
        "404 sin json",
        "404 lista",
        "500",
        "fuera del contrato",
    ],
)
async def test_respuestas_de_catalogo(
    catalogo: Catalogo, reloj: Reloj, respuesta: httpx.Response, error: type[Exception]
) -> None:
    catalogo.respuestas["/vigente"] = respuesta
    with pytest.raises(error):
        await adaptador(catalogo, reloj, ttl_s=0).reglas_vigentes("vida-hipotecario", AHORA)


async def test_sin_conexion_es_dependencia_no_disponible(catalogo: Catalogo, reloj: Reloj) -> None:
    catalogo.error = httpx.ConnectError("sin red")
    with pytest.raises(DependenciaNoDisponible) as error:
        await adaptador(catalogo, reloj, ttl_s=0).producto("vida-hipotecario")
    assert error.value.dependencia == "catalogo.productos"


async def test_un_factor_fuera_del_contrato_se_rechaza(catalogo: Catalogo, reloj: Reloj) -> None:
    reglas = reglas_json()
    reglas["definicion"]["factoresVariables"][0]["variable"] = "ingresos"
    catalogo.respuestas["/vigente"] = httpx.Response(200, json=reglas)
    with pytest.raises(DependenciaNoDisponible):
        await adaptador(catalogo, reloj, ttl_s=0).reglas_vigentes("vida-hipotecario", AHORA)


# --- Sin CACHE_URL: caché en memoria -------------------------------------------------------


async def test_cache_en_memoria_con_ttl(catalogo: Catalogo, reloj: Reloj) -> None:
    productos = adaptador(catalogo, reloj, ttl_s=60)
    assert (await productos.producto("vida-hipotecario")).origen is OrigenDato.CATALOGO
    assert (await productos.producto("vida-hipotecario")).origen is OrigenDato.MEMORIA
    reloj.instante = AHORA + timedelta(seconds=61)
    assert (await productos.producto("vida-hipotecario")).origen is OrigenDato.CATALOGO
    assert len(catalogo.solicitudes) == 2


async def test_en_memoria_no_usa_reglas_posteriores_a_la_fecha(
    catalogo: Catalogo, reloj: Reloj
) -> None:
    productos = adaptador(catalogo, reloj, ttl_s=60)
    await productos.reglas_vigentes("vida-hipotecario", AHORA)
    antes = datetime(2026, 8, 31, tzinfo=UTC)
    catalogo.respuestas["/vigente"] = httpx.Response(404, json={"codigo": "REGLAS_NO_VIGENTES"})
    with pytest.raises(ReglasNoVigentes):
        await productos.reglas_vigentes("vida-hipotecario", antes)


async def test_solicitudes_simultaneas_en_frio_piden_una_vez(
    catalogo: Catalogo, reloj: Reloj
) -> None:
    import asyncio

    productos = adaptador(catalogo, reloj, ttl_s=60)
    resultados = await asyncio.gather(*(productos.producto("vida-hipotecario") for _ in range(5)))
    assert len(catalogo.solicitudes) == 1
    assert {r.origen for r in resultados} == {OrigenDato.CATALOGO, OrigenDato.MEMORIA}


# --- Con CACHE_URL: solo lectura de Valkey ---------------------------------------------------


async def test_lee_valkey_y_nunca_escribe(catalogo: Catalogo, reloj: Reloj) -> None:
    cache = CacheLectura(
        {
            CLAVE_PRODUCTO: json.dumps(producto_json()).encode(),
            CLAVE_REGLAS: json.dumps(reglas_json()).encode(),
        }
    )
    productos = adaptador(catalogo, reloj, cache=cache)
    producto = await productos.producto("vida-hipotecario")
    reglas = await productos.reglas_vigentes("vida-hipotecario", AHORA)
    assert (producto.origen, reglas.origen) == (OrigenDato.CACHE, OrigenDato.CACHE)
    assert catalogo.solicitudes == [] and cache.escrituras == 0


async def test_fallo_de_valkey_va_a_catalogo_sin_escribir(catalogo: Catalogo, reloj: Reloj) -> None:
    cache = CacheLectura({CLAVE_PRODUCTO: b"{no es json"})
    productos = adaptador(catalogo, reloj, cache=cache)
    assert (await productos.producto("vida-hipotecario")).origen is OrigenDato.CATALOGO
    assert (await productos.reglas_vigentes("vida-hipotecario", AHORA)).origen is (
        OrigenDato.CATALOGO
    )
    assert len(catalogo.solicitudes) == 2 and cache.escrituras == 0


async def test_valkey_con_reglas_posteriores_a_la_fecha_va_a_catalogo(
    catalogo: Catalogo, reloj: Reloj
) -> None:
    cache = CacheLectura({CLAVE_REGLAS: json.dumps(reglas_json("2026-10-16T00:00:00Z")).encode()})
    reglas = await adaptador(catalogo, reloj, cache=cache).reglas_vigentes(
        "vida-hipotecario", AHORA
    )
    assert reglas.origen is OrigenDato.CATALOGO
