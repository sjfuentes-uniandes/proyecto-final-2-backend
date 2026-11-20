"""Arnés de las pruebas de módulo de adaptador-telemetria (HU-W21, MT-W21-01): PostgreSQL efímero
(Testcontainers) con las migraciones del servicio y LocalStack 4.14.0 con S3, SNS y SQS.

`limpia` vacía las tablas antes de cada prueba; `bucket` crea un bucket nuevo por prueba (así
contar objetos no depende de las demás); `servicio` arma la app real contra esa base y ese bucket.

psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: las pruebas corren en un
SelectorEventLoop (en Linux es el bucle por defecto), como `dinero`, `polizas` y `siniestros`.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import boto3
import httpx
import psycopg
import pytest
from adaptador_telemetria.adapters.outbound.base_de_datos import migrar
from adaptador_telemetria.config import Settings
from adaptador_telemetria.main import crear_app
from botocore.config import Config

docker = pytest.importorskip("docker")

IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
REGION = "us-east-1"
SECRETO = "secreto-de-prueba"  # el mismo con el que firma `firmar_caso`


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


def ejecutar(url: str, sql: str, *parametros: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.execute(sql, parametros)
        return cursor.fetchall() if cursor.description else []


@pytest.fixture
def sql() -> Callable[..., list[tuple[Any, ...]]]:
    """Ejecuta SQL directo sobre la base (solo para verificar lo que dejó el servicio)."""
    return ejecutar


@pytest.fixture
def secreto() -> str:
    return SECRETO


@pytest.fixture
def limpia(base: str) -> str:
    ejecutar(base, "TRUNCATE eventos_recibidos, outbox, inbox")
    return base


@pytest.fixture(scope="module")
def localstack() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.localstack import LocalStackContainer

    with LocalStackContainer(image=IMAGEN_LOCALSTACK, region_name=REGION).with_services(
        "s3", "sns", "sqs"
    ) as contenedor:
        yield contenedor.get_url()


@pytest.fixture
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    # LocalStack acepta cualquier credencial; boto3 usa la cadena por defecto.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@dataclass
class Bucket:
    s3: Any
    nombre: str

    def claves(self) -> list[str]:
        paginas = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.nombre)
        return sorted(o["Key"] for p in paginas for o in p.get("Contents", []))

    def leer(self, clave: str) -> bytes:
        cuerpo: bytes = self.s3.get_object(Bucket=self.nombre, Key=clave)["Body"].read()
        return cuerpo

    def cabecera(self, clave: str) -> dict[str, Any]:
        cabecera: dict[str, Any] = self.s3.head_object(
            Bucket=self.nombre, Key=clave, ChecksumMode="ENABLED"
        )
        return cabecera


@pytest.fixture
def bucket(localstack: str, credenciales: None) -> Bucket:
    s3 = boto3.client(
        "s3",
        region_name=REGION,
        endpoint_url=localstack,
        config=Config(s3={"addressing_style": "path"}),
    )
    nombre = f"evidencias-{uuid4().hex[:12]}"
    s3.create_bucket(Bucket=nombre)
    return Bucket(s3, nombre)


@dataclass
class Servicio:
    """La app real (lifespan arrancado) y un cliente HTTP en proceso."""

    app: Any
    cliente: httpx.AsyncClient
    settings: Settings


@pytest.fixture
def credenciales_hmac() -> str:
    return json.dumps({"hmac_secret": SECRETO})


@pytest.fixture
def servicio(
    limpia: str, localstack: str, bucket: Bucket, credenciales_hmac: str
) -> Callable[..., Any]:
    """Fábrica de `async with servicio(**ajustes) as s`: ajustes extra de `Settings`."""

    @asynccontextmanager
    async def crear(**ajustes: Any) -> AsyncIterator[Servicio]:
        valores: dict[str, Any] = {
            "SOLVENTA_DATABASE_URL": limpia,
            "ALLY_TELEMETRIA_CREDENTIALS": credenciales_hmac,
            "EVIDENCIAS_BUCKET": bucket.nombre,
            "SOLVENTA_AWS_ENDPOINT_URL": localstack,
            "SOLVENTA_DB_POOL_SIZE": 20,
            **ajustes,
        }
        settings = Settings(**valores)
        app = crear_app(settings)
        async with app.router.lifespan_context(app):
            transporte = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transporte, base_url="http://test") as cliente:
                yield Servicio(app, cliente, settings)

    return crear
