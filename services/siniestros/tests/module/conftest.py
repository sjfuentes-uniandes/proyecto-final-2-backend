"""Arnés de las pruebas de módulo de siniestros (HU-W19): PostgreSQL efímero (Testcontainers) con
las migraciones del servicio.

`base` migra una vez por módulo de pruebas; `limpia` vacía las tablas antes de cada prueba y `conexion`
abre una conexión autocommit sobre ella. Se omiten si Docker no está disponible.
"""

import asyncio
import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from siniestros.adapters.outbound.base_de_datos import migrar

docker = pytest.importorskip("docker")

TABLAS = (
    "siniestros, evidencias, historial_siniestro, outbox, inbox, idempotencia, secuencia_codigo"
)


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


@pytest.fixture
def limpia(base: str) -> str:
    # TRUNCATE no dispara el disparador de solo inserción (es por fila): solo para las pruebas.
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(f"TRUNCATE {TABLAS}")
    return base


@pytest.fixture
def conexion(limpia: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(limpia, autocommit=True) as abierta:
        yield abierta
