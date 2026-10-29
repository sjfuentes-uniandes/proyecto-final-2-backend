"""Arnés de las pruebas de módulo de dinero (HU-W16, 16.9): PostgreSQL efímero (Testcontainers) con
las migraciones del servicio.

`limpia` vacía las tablas antes de cada prueba; `pool` es un `AsyncConnectionPool` sobre esa base y
`recaudos`, el repositorio sobre ese pool.

psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: las pruebas corren en un
SelectorEventLoop (en Linux es el bucle por defecto), como `polizas`, `cotizacion` y `bff-movil`.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg
import pytest
from dinero.adapters.outbound.recaudos_pg import RecaudosPg, migrar

docker = pytest.importorskip("docker")


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # psycopg async no corre sobre el ProactorEventLoop de Windows.
    return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}


def _docker_disponible() -> None:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver=None) as postgres:
        url: str = postgres.get_connection_url()
        migrar(url)
        yield url


def _ejecutar(url: str, sql: str) -> None:
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(sql)


@pytest.fixture
def limpia(base: str) -> str:
    # TRUNCATE no dispara el disparador de protección (es por fila): solo para las pruebas.
    _ejecutar(base, "TRUNCATE recaudos, outbox, inbox")
    return base


@pytest.fixture
async def pool(limpia: str) -> AsyncIterator[Any]:
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(limpia, min_size=1, max_size=25, open=False) as abierto:
        yield abierto


@pytest.fixture
def recaudos(pool: Any) -> RecaudosPg:
    return RecaudosPg(pool)
