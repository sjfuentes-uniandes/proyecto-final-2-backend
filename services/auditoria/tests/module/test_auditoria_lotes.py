"""HU-W24 (N2): `auditoria` por lotes contra PostgreSQL, SQS y S3 reales (Testcontainers).

La cola tiene DLQ (`maxReceiveCount` = 5) y el bucket Object Lock y versionado, como
`infra/platform/storage.tf`. Varias tareas consumen a la vez: la cadena queda en serie (cabeza
bloqueada), sin huecos ni registros repetidos, y verifica de punta a punta.

`TESTCONTAINERS_RYUK_DISABLED=true` en macOS.
"""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from auditoria.adapters.inbound.sqs.lotes import ConsumidorLotes
from auditoria.adapters.outbound.almacen_s3 import PREFIJO, AlmacenS3, cliente_s3
from auditoria.adapters.outbound.cadena_pg import CadenaPg, migrar
from auditoria.adapters.outbound.reloj import RelojSistema
from auditoria.application.registrar_lote import RegistrarLote
from auditoria.domain.registro import Lote, verificar_cadena
from botocore.exceptions import ClientError
from psycopg_pool import AsyncConnectionPool
from solventa_common.inbox import ReceptorSqs, cliente_sqs

docker = pytest.importorskip("docker")

IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
REGION = "us-east-1"
ESPERA_LARGA_S = 1
MAX_RECEPCIONES = 5


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
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
        migrar(url)  # idempotente
        yield url


@pytest.fixture(scope="module")
def localstack() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.localstack import LocalStackContainer

    with LocalStackContainer(image=IMAGEN_LOCALSTACK, region_name=REGION).with_services(
        "sqs", "s3"
    ) as contenedor:
        yield contenedor.get_url()


@pytest.fixture
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def limpia(base: str) -> str:
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE inbox")
        conexion.execute("UPDATE cadena SET secuencia = 0, hash = repeat('0', 64) WHERE id = 1")
    return base


@dataclass
class Entorno:
    sqs: Any
    s3: Any
    cola: str
    dlq: str
    bucket: str

    def enviar(self, cuerpo: str) -> None:
        self.sqs.send_message(
            QueueUrl=self.cola,
            MessageBody=cuerpo,
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": "PolizaEmitida"}},
        )

    def contar(self, url: str | None = None) -> tuple[int, int]:
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=url or self.cola,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return (
            int(atributos["ApproximateNumberOfMessages"]),
            int(atributos["ApproximateNumberOfMessagesNotVisible"]),
        )

    def receptor(self) -> ReceptorSqs:
        return ReceptorSqs(self.sqs, self.cola, espera_larga_s=ESPERA_LARGA_S, max_mensajes=10)

    def almacen(self) -> AlmacenS3:
        return AlmacenS3(self.s3, self.bucket)

    def objetos(self) -> list[str]:
        respuesta = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=PREFIJO)
        return sorted(o["Key"] for o in respuesta.get("Contents", []))


@pytest.fixture
def entorno(localstack: str, credenciales: None) -> Entorno:
    sqs = cliente_sqs(REGION, endpoint_url=localstack, espera_larga_s=ESPERA_LARGA_S)
    s3 = cliente_s3(REGION, endpoint_url=localstack)
    sufijo = uuid4().hex[:8]
    dlq = sqs.create_queue(QueueName=f"auditoria-{sufijo}-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    cola = sqs.create_queue(
        QueueName=f"auditoria-{sufijo}",
        Attributes={
            "VisibilityTimeout": "2",
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEPCIONES}
            ),
        },
    )["QueueUrl"]
    bucket = f"auditoria-{sufijo}"
    s3.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
    s3.put_object_lock_configuration(
        Bucket=bucket,
        ObjectLockConfiguration={
            "ObjectLockEnabled": "Enabled",
            "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 1}},
        },
    )
    return Entorno(sqs=sqs, s3=s3, cola=cola, dlq=dlq, bucket=bucket)


@asynccontextmanager
async def abrir_pool(url: str) -> AsyncIterator[AsyncConnectionPool[Any]]:
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def sobre(event_id: str | None = None) -> str:
    return json.dumps(
        {
            "eventId": event_id or str(uuid4()),
            "aggregateId": str(uuid4()),
            "type": "PolizaEmitida",
            "version": 1,
            "occurredAt": "2026-11-05T14:06:12Z",
            "correlationId": "corr-w24-01",
            "payload": {"actorId": "asesor-sintetico-01"},
        }
    )


def cabeza(url: str) -> tuple[int, str]:
    with psycopg.connect(url) as conexion:
        fila = conexion.execute("SELECT secuencia, hash FROM cadena WHERE id = 1").fetchone()
        assert fila is not None
        return int(fila[0]), str(fila[1])


async def correr(
    consumidores: list[ConsumidorLotes], entorno: Entorno, limite_s: float = 40
) -> None:
    parada = asyncio.Event()
    tareas = [asyncio.create_task(c.ejecutar(parada)) for c in consumidores]
    limite = time.monotonic() + limite_s
    try:
        while entorno.contar() != (0, 0):
            assert time.monotonic() < limite, "la cola no se drenó a tiempo"
            await asyncio.sleep(0.2)
    finally:
        parada.set()
        await asyncio.gather(*tareas)


def _consumidor(pool: Any, entorno: Entorno, almacen: Any = None) -> ConsumidorLotes:
    caso = RegistrarLote(CadenaPg(pool), almacen or entorno.almacen(), RelojSistema())
    return ConsumidorLotes(entorno.receptor(), caso)


@pytest.mark.anyio
async def test_una_avalancha_con_varias_tareas_queda_en_una_cadena_sin_huecos_ni_repetidos(
    limpia: str, entorno: Entorno
) -> None:
    enviados = [str(uuid4()) for _ in range(95)]
    for event_id in enviados:
        entorno.enviar(sobre(event_id))
    # Duplicados: el mismo eventId reentregado.
    for event_id in enviados[:15]:
        entorno.enviar(sobre(event_id))

    async with abrir_pool(limpia) as pool:
        await correr([_consumidor(pool, entorno) for _ in range(3)], entorno)

    secuencia, hash_ = cabeza(limpia)
    documentos = await entorno.almacen().leer_cadena(secuencia, hash_)
    assert verificar_cadena(documentos) == hash_
    registrados = [r["eventId"] for d in documentos for r in d["registros"]]
    assert sorted(registrados) == sorted(enviados)
    # Un objeto por lote recibido: nunca más de 10 registros por objeto.
    assert len(entorno.objetos()) == secuencia >= 10
    assert all(1 <= len(d["registros"]) <= 10 for d in documentos)
    assert entorno.contar(entorno.dlq) == (0, 0)


@pytest.mark.anyio
async def test_el_objeto_no_se_reemplaza(limpia: str, entorno: Entorno) -> None:
    entorno.enviar(sobre())
    async with abrir_pool(limpia) as pool:
        await correr([_consumidor(pool, entorno)], entorno)
    (clave,) = entorno.objetos()

    with pytest.raises(ClientError) as error:
        entorno.s3.put_object(Bucket=entorno.bucket, Key=clave, Body=b"{}", IfNoneMatch="*")

    assert error.value.response["Error"]["Code"] in {"PreconditionFailed", "412"}


class AlmacenQueCaeTrasEscribir:
    """Escribe el objeto y falla antes del commit, una vez: queda un objeto huérfano."""

    def __init__(self, real: AlmacenS3) -> None:
        self.real = real
        self.caidas = 1

    async def guardar(self, lote: Lote) -> None:
        await self.real.guardar(lote)
        if self.caidas:
            self.caidas -= 1
            raise ConnectionError("el proceso cae antes del commit")


@pytest.mark.anyio
async def test_un_lote_cuyo_commit_falla_se_reescribe_y_el_huerfano_queda_fuera_de_la_cadena(
    limpia: str, entorno: Entorno
) -> None:
    enviados = [str(uuid4()) for _ in range(4)]
    for event_id in enviados:
        entorno.enviar(sobre(event_id))

    async with abrir_pool(limpia) as pool:
        almacen = AlmacenQueCaeTrasEscribir(entorno.almacen())
        await correr([_consumidor(pool, entorno, almacen)], entorno)

    secuencia, hash_ = cabeza(limpia)
    documentos = await entorno.almacen().leer_cadena(secuencia, hash_)
    assert verificar_cadena(documentos) == hash_
    assert sorted(r["eventId"] for d in documentos for r in d["registros"]) == sorted(enviados)
    # Un objeto más que la cadena: el huérfano, con la secuencia 1 y otro hash.
    assert len(entorno.objetos()) == secuencia + 1


@pytest.mark.anyio
async def test_un_mensaje_envenenado_termina_en_la_dlq_sin_frenar_el_lote(
    limpia: str, entorno: Entorno
) -> None:
    for _ in range(3):
        entorno.enviar(sobre())
    entorno.enviar("{no es un sobre")

    async with abrir_pool(limpia) as pool:
        await correr([_consumidor(pool, entorno)], entorno)

    secuencia, hash_ = cabeza(limpia)
    documentos = await entorno.almacen().leer_cadena(secuencia, hash_)
    assert sum(len(d["registros"]) for d in documentos) == 3
    assert entorno.contar(entorno.dlq) == (1, 0)
