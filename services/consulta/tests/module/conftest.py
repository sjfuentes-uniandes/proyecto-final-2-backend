"""Arnés de las pruebas de módulo de consulta (HU-M10): PostgreSQL efímero (Testcontainers) con las
migraciones aplicadas.

psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: las pruebas corren en un
SelectorEventLoop (en Linux es el bucle por defecto), como `polizas`.
"""

import asyncio
import os
from collections.abc import Iterator
from typing import Any

import pytest
from consulta.adapters.outbound.polizas_pg import migrar

docker = pytest.importorskip("docker")


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver=None) as postgres:
        url: str = postgres.get_connection_url()
        migrar(url)
        yield url
