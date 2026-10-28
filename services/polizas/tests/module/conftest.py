"""Arnés de las pruebas de módulo de polizas (HU-W13, compartido desde HU-W14): PostgreSQL efímero
(Testcontainers) con las migraciones, y la app con dobles HTTP de cotizacion y catalogo (transporte
simulado de httpx con los contratos publicados).

`api` es la app sobre la base limpia; `abrir_api` abre otra instancia sobre la misma base (con
cachés nuevas), para probar lecturas con los proveedores en otro estado.

psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: las pruebas corren en un
SelectorEventLoop (en Linux es el bucle por defecto), como `cotizacion`, `bff-movil` y `clientes`.
"""

import asyncio
import os
import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
import psycopg
import pytest
from polizas.adapters.outbound.aceptaciones_pg import AceptacionesPg
from polizas.adapters.outbound.catalogo_http import CatalogoHttp
from polizas.adapters.outbound.codigos import CodigosPg
from polizas.adapters.outbound.cotizacion_http import CotizacionHttp
from polizas.adapters.outbound.decisiones_pg import DecisionesPg, migrar
from polizas.config import Settings
from polizas.main import Puertos, crear_app

docker = pytest.importorskip("docker")


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # psycopg async no corre sobre el ProactorEventLoop de Windows.
    return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from testcontainers.community.postgres import PostgresContainer

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    with PostgresContainer("postgres:16", driver=None) as postgres:
        url: str = postgres.get_connection_url()
        migrar(url)
        yield url


def _ejecutar(url: str, sql: str) -> None:
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(sql)


@pytest.fixture
def limpia(base: str) -> str:
    # TRUNCATE no dispara el disparador de inmutabilidad (es por fila): solo para las pruebas.
    _ejecutar(base, "TRUNCATE aceptaciones, decisiones_suscripcion, outbox")
    return base


class Proveedores:
    """Dobles HTTP de cotizacion y catalogo, con su estado (nominal, caído o lento)."""

    def __init__(self, sinteticos: Any) -> None:
        self.insumos = {
            c["insumos"]["cotizacionId"]: c["insumos"] for c in sinteticos.datos["casos"]
        }
        self.reglas = sinteticos.respuesta_reglas()
        self.cotizacion = "nominal"
        self.catalogo = "nominal"

    async def responder(self, pedido: httpx.Request) -> httpx.Response:
        estado = self.cotizacion if pedido.url.host == "cotizacion" else self.catalogo
        if estado == "caido":
            return httpx.Response(503, json={"codigo": "SERVICIO_NO_DISPONIBLE"})
        if estado == "lento":
            await asyncio.sleep(2)
        if pedido.url.host == "catalogo":
            return httpx.Response(200, json=self.reglas)
        cotizacion = re.search(r"/v1/cotizaciones/([^/]+)/", pedido.url.path)
        insumos = self.insumos.get(cotizacion.group(1) if cotizacion else "")
        if insumos is None:
            return httpx.Response(404, json={"codigo": "COTIZACION_NO_ENCONTRADA"})
        return httpx.Response(200, json=insumos)


@asynccontextmanager
async def _api(url: str, proveedores: Proveedores, reloj: Any) -> AsyncIterator[httpx.AsyncClient]:
    from psycopg_pool import AsyncConnectionPool

    settings = Settings(SOLVENTA_DATABASE_URL=url)
    async with (
        AsyncConnectionPool(url, min_size=1, max_size=10, open=False) as pool,
        httpx.AsyncClient(transport=httpx.MockTransport(proveedores.responder)) as salida,
    ):
        puertos = Puertos(
            insumos=CotizacionHttp(salida, "http://cotizacion", settings.cotizacion_timeout_ms),
            reglas=CatalogoHttp(salida, "http://catalogo", settings.catalogo_timeout_ms, reloj),
            decisiones=DecisionesPg(pool),
            codigos=CodigosPg(pool),
            reloj=reloj,
            aceptaciones=AceptacionesPg(pool),
        )
        app = crear_app(settings, puertos=puertos)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://polizas"
            ) as cliente,
        ):
            yield cliente


@pytest.fixture
async def api(limpia: str, sinteticos: Any, dobles: Any) -> AsyncIterator[Any]:
    proveedores = Proveedores(sinteticos)
    async with _api(limpia, proveedores, dobles.reloj) as cliente:
        cliente.proveedores = proveedores  # type: ignore[attr-defined]
        yield cliente


@pytest.fixture
def abrir_api(
    limpia: str, dobles: Any
) -> Callable[[Proveedores], AbstractAsyncContextManager[httpx.AsyncClient]]:
    """Otra instancia de la app sobre la misma base, con los proveedores dados."""
    return lambda proveedores: _api(limpia, proveedores, dobles.reloj)
