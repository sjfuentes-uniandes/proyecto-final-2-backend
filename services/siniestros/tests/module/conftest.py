"""Arnés de las pruebas de módulo de siniestros (HU-W19): PostgreSQL efímero (Testcontainers) con
las migraciones del servicio.

`base` migra una vez por módulo de pruebas; `limpia` vacía las tablas antes de cada prueba y `conexion`
abre una conexión autocommit sobre ella. Se omiten si Docker no está disponible.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

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


# --- Pool, LocalStack y S3 -------------------------------------------------------------------------
# La imagen del equipo; `SINIESTROS_LOCALSTACK_IMAGE` permite usar otra ya descargada (p. ej. la
# `localstack/localstack:3` de compose.yaml) cuando el registro no está disponible.
IMAGEN_LOCALSTACK = os.environ.get("SINIESTROS_LOCALSTACK_IMAGE", "localstack/localstack:4.14.0")
REGION = "us-east-1"
SSE = "AES256"


@pytest.fixture
async def pool(limpia: str) -> AsyncIterator[Any]:
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(limpia, min_size=1, max_size=25, open=False) as abierto:
        yield abierto


@pytest.fixture(scope="module")
def localstack() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.localstack import LocalStackContainer

    # S3_SKIP_SIGNATURE_VALIDATION=0: LocalStack valida firma y caducidad de las URL prefirmadas como
    # S3 real (por omisión las omite); MT-W19-01 depende de ello.
    contenedor = (
        LocalStackContainer(image=IMAGEN_LOCALSTACK, region_name=REGION)
        .with_services("s3", "sns", "sqs")
        .with_env("S3_SKIP_SIGNATURE_VALIDATION", "0")
    )
    with contenedor:
        yield contenedor.get_url()


@pytest.fixture
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    # LocalStack acepta cualquier credencial; boto3 usa la cadena por defecto.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def s3(localstack: str, credenciales: None) -> Any:
    """Un cliente S3 de pruebas (sin corte corto) contra LocalStack."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=REGION,
        endpoint_url=localstack,
        config=Config(s3={"addressing_style": "path"}),
    )


@pytest.fixture
def bucket(s3: Any) -> str:
    nombre = f"evidencias-{uuid4().hex[:12]}"
    s3.create_bucket(Bucket=nombre)
    return nombre
