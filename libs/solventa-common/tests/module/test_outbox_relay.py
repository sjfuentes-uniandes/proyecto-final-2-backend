"""Relay del outbox (HU-W16, 16.4) contra PostgreSQL y LocalStack reales (Testcontainers).

Un tópico con dos colas suscritas con entrega en crudo y los filtros de `infra/platform/catalog.tf`
(`dinero-aceptaciones`: `OfertaAceptada` exacto; `polizas-recaudos`: prefijo `Recaudo`). Casos:
enrutamiento por `eventType`, backlog por lotes, caída entre publicar y el commit (duplicado con el
mismo `eventId`), SNS inalcanzable y dos relays concurrentes sobre la misma tabla.

psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: las pruebas corren en un
SelectorEventLoop (en Linux es el bucle por defecto), como `polizas/tests/module`.
"""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import boto3
import psycopg
import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from solventa_common.outbox import (
    OutboxPg,
    PublicadorSns,
    RelayOutbox,
    TransaccionOutboxPg,
    cliente_sns,
)

docker = pytest.importorskip("docker")

IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
REGION = "us-east-1"
EVENTOS = Path(__file__).resolve().parents[4] / "contracts" / "events"
INICIO = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)

# Copiado de services/polizas/src/polizas/adapters/outbound/migraciones/001_suscripcion.sql
# (tabla `outbox` e índice `outbox_pendientes`): el relay se prueba con la forma real de la fila.
DDL_OUTBOX = """
CREATE TABLE IF NOT EXISTS outbox (
  event_id uuid PRIMARY KEY,
  aggregate_id uuid NOT NULL,
  event_type text NOT NULL,
  event_version int NOT NULL,
  occurred_at timestamptz NOT NULL,
  correlation_id text NOT NULL,
  payload jsonb NOT NULL,
  published_at timestamptz NULL,
  attempts int NOT NULL DEFAULT 0,
  last_error text NULL
);
CREATE INDEX IF NOT EXISTS outbox_pendientes ON outbox (occurred_at) WHERE published_at IS NULL;
"""

# Filtros de infra/platform/catalog.tf (dinero-aceptaciones y polizas-recaudos).
FILTRO_ACEPTACIONES = {"eventType": ["OfertaAceptada"]}
FILTRO_RECAUDOS = {"eventType": [{"prefix": "Recaudo"}]}


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
        with psycopg.connect(url, autocommit=True) as conexion:
            conexion.execute(DDL_OUTBOX)
        yield url


@pytest.fixture(scope="module")
def localstack() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.localstack import LocalStackContainer

    with LocalStackContainer(image=IMAGEN_LOCALSTACK, region_name=REGION).with_services(
        "sns", "sqs"
    ) as contenedor:
        yield contenedor.get_url()


@pytest.fixture
def limpia(base: str) -> str:
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE outbox")
    return base


@pytest.fixture
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    # LocalStack acepta cualquier credencial; el relay usa la cadena por defecto de boto3.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@dataclass
class Mensajeria:
    """Un tópico nuevo por prueba con las dos colas filtradas de catalog.tf."""

    sns: Any
    sqs: Any
    topic_arn: str
    aceptaciones: str
    recaudos: str

    def publicador(self) -> PublicadorSns:
        return PublicadorSns(self.sns, self.topic_arn)

    def recibir(self, cola: str, cantidad: int, espera_max_s: float = 30) -> list[dict[str, Any]]:
        """Recibe y borra hasta `cantidad` mensajes."""
        mensajes: list[dict[str, Any]] = []
        limite = time.monotonic() + espera_max_s
        while len(mensajes) < cantidad and time.monotonic() < limite:
            respuesta = self.sqs.receive_message(
                QueueUrl=cola,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=1,
                MessageAttributeNames=["All"],
            )
            for mensaje in respuesta.get("Messages", []):
                mensajes.append(mensaje)
                self.sqs.delete_message(QueueUrl=cola, ReceiptHandle=mensaje["ReceiptHandle"])
        return mensajes

    def vacia(self, cola: str) -> bool:
        respuesta = self.sqs.receive_message(QueueUrl=cola, WaitTimeSeconds=2)
        return not respuesta.get("Messages")


@pytest.fixture
def mensajeria(localstack: str, credenciales: None) -> Mensajeria:
    sns = cliente_sns(REGION, endpoint_url=localstack)
    sqs = boto3.client("sqs", region_name=REGION, endpoint_url=localstack)
    sufijo = uuid4().hex[:8]
    topic_arn = sns.create_topic(Name=f"eventos-{sufijo}")["TopicArn"]

    def cola(nombre: str, filtro: dict[str, Any]) -> str:
        url: str = sqs.create_queue(QueueName=f"{nombre}-{sufijo}")["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        sns.subscribe(
            TopicArn=topic_arn,
            Protocol="sqs",
            Endpoint=arn,
            Attributes={"RawMessageDelivery": "true", "FilterPolicy": json.dumps(filtro)},
        )
        return url

    return Mensajeria(
        sns=sns,
        sqs=sqs,
        topic_arn=topic_arn,
        aceptaciones=cola("dinero-aceptaciones", FILTRO_ACEPTACIONES),
        recaudos=cola("polizas-recaudos", FILTRO_RECAUDOS),
    )


@asynccontextmanager
async def abrir_pool(url: str) -> AsyncIterator[AsyncConnectionPool[Any]]:
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


# --- Datos -------------------------------------------------------------------------------------------
def _payload(tipo: str) -> dict[str, Any]:
    if tipo == "OfertaAceptada":
        aceptacion = str(uuid4())
        return {
            "aceptacionId": aceptacion,
            "cotizacionId": str(uuid4()),
            "ofertaId": str(uuid4()),
            "decisionId": str(uuid4()),
            "clienteId": str(uuid4()),
            "productoId": "VIDA-INDIVIDUAL",
            "prima": {"valor": "1850000.50", "moneda": "COP", "periodicidad": "ANUAL"},
            "claveRecaudo": aceptacion,
            "huellaCalculo": "sha256:abc",
            "vigenteHasta": "2026-11-27T13:05:26Z",
            "versionDeclaraciones": "2026-10",
            "aceptadaEn": "2026-10-28T13:05:26.000120Z",
            "actorId": "asesor-sintetico-01",
            "canal": "PORTAL_ASESOR",
        }
    if tipo == "DecisionSuscripcionRegistrada":
        return {
            "decisionId": str(uuid4()),
            "cotizacionId": str(uuid4()),
            "ofertaId": None,
            "clienteId": str(uuid4()),
            "productoId": "VIDA-INDIVIDUAL",
            "resultado": "APROBADA",
            "tipo": "AUTOMATICA",
            "reglaDeterminante": None,
            "reglasNoCumplidas": [],
            "versionReglas": "2026.10",
            "hashReglas": "sha256:reglas",
            "hashInsumos": "sha256:insumos",
            "actorId": "asesor-sintetico-01",
            "decididaEn": "2026-10-28T13:05:26Z",
        }
    # Recaudo*: aún sin esquema publicado (16.10); basta un payload sintético.
    return {"claveRecaudo": str(uuid4()), "valor": "1850000.50"}


def insertar(url: str, tipos: list[str]) -> list[UUID]:
    """Inserta un evento por tipo, con `occurred_at` creciente; devuelve sus `event_id`."""
    ids = [uuid4() for _ in tipos]
    with psycopg.connect(url) as conexion, conexion.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO outbox (event_id, aggregate_id, event_type, event_version, occurred_at, "
            "correlation_id, payload) VALUES (%s, %s, %s, 1, %s, %s, %s)",
            [
                (
                    event_id,
                    uuid4(),
                    tipo,
                    INICIO + timedelta(milliseconds=i),
                    f"corr-{i}",
                    Jsonb(_payload(tipo)),
                )
                for i, (event_id, tipo) in enumerate(zip(ids, tipos, strict=True))
            ],
        )
    return ids


def filas(url: str) -> dict[UUID, tuple[Any, ...]]:
    """event_id -> (published_at, attempts, last_error)."""
    with psycopg.connect(url) as conexion:
        consulta = "SELECT event_id, published_at, attempts, last_error FROM outbox"
        return {fila[0]: fila[1:] for fila in conexion.execute(consulta).fetchall()}


def _event_ids(mensajes: list[dict[str, Any]]) -> list[str]:
    return [json.loads(m["Body"])["eventId"] for m in mensajes]


def _validar(sobre: dict[str, Any]) -> None:
    esquema = json.loads((EVENTOS / f"{sobre['type']}.v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(
        sobre
    )


# --- Casos ---------------------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_cada_cola_recibe_solo_lo_suyo_con_el_sobre_y_queda_marcado(
    limpia: str, mensajeria: Mensajeria
) -> None:
    tipos = ["OfertaAceptada", "DecisionSuscripcionRegistrada", "RecaudoConfirmado"]
    oferta, decision, recaudo = insertar(limpia, tipos)

    async with abrir_pool(limpia) as pool:
        relay = RelayOutbox(OutboxPg(pool), mensajeria.publicador())
        assert await relay.ejecutar_una_vez() == 3

    aceptaciones = mensajeria.recibir(mensajeria.aceptaciones, 1)
    recaudos = mensajeria.recibir(mensajeria.recaudos, 1)
    assert _event_ids(aceptaciones) == [str(oferta)]
    assert _event_ids(recaudos) == [str(recaudo)]
    assert mensajeria.vacia(mensajeria.aceptaciones)
    assert mensajeria.vacia(mensajeria.recaudos)

    sobre = json.loads(aceptaciones[0]["Body"])
    _validar(sobre)
    assert sobre["type"] == "OfertaAceptada"
    assert sobre["correlationId"] == "corr-0"
    assert sobre["occurredAt"] == "2026-10-28T13:00:00Z"
    assert aceptaciones[0]["MessageAttributes"]["eventType"] == {
        "DataType": "String",
        "StringValue": "OfertaAceptada",
    }
    assert json.loads(recaudos[0]["Body"])["type"] == "RecaudoConfirmado"

    estado = filas(limpia)
    for event_id in (oferta, decision, recaudo):
        published_at, attempts, last_error = estado[event_id]
        assert published_at is not None
        assert (attempts, last_error) == (0, None)


@pytest.mark.anyio
async def test_un_backlog_de_250_se_publica_en_lotes_por_orden(
    limpia: str, mensajeria: Mensajeria
) -> None:
    ids = insertar(limpia, ["RecaudoConfirmado"] * 250)

    async with abrir_pool(limpia) as pool:
        relay = RelayOutbox(OutboxPg(pool), mensajeria.publicador(), lote=50)
        primero = await relay.ejecutar_una_vez()
        publicados_primero = {i for i, (p, _, _) in filas(limpia).items() if p is not None}
        resto = [await relay.ejecutar_una_vez() for _ in range(5)]

    assert [primero, *resto] == [50, 50, 50, 50, 50, 0]
    assert publicados_primero == set(ids[:50])
    assert all(published_at is not None for published_at, _, _ in filas(limpia).values())
    recibidos = _event_ids(mensajeria.recibir(mensajeria.recaudos, 250))
    assert sorted(recibidos) == sorted(str(i) for i in ids)


class CaidaAntesDelCommit:
    """Publica de verdad y luego corta la conexión de la transacción: el commit falla."""

    def __init__(self, real: OutboxPg, url: str) -> None:
        self._real = real
        self._url = url

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[TransaccionOutboxPg]:
        async with self._real.transaccion() as tx:
            yield tx
            pid = tx.conexion.info.backend_pid
            with psycopg.connect(self._url, autocommit=True) as otra:
                otra.execute("SELECT pg_terminate_backend(%s)", (pid,))


@pytest.mark.anyio
async def test_si_el_commit_falla_tras_publicar_se_republica_con_el_mismo_event_id(
    limpia: str, mensajeria: Mensajeria
) -> None:
    (event_id,) = insertar(limpia, ["OfertaAceptada"])

    async with abrir_pool(limpia) as pool:
        caido = RelayOutbox(CaidaAntesDelCommit(OutboxPg(pool), limpia), mensajeria.publicador())
        with pytest.raises(psycopg.OperationalError):
            await caido.ejecutar_una_vez()
        assert filas(limpia)[event_id][0] is None

        assert await RelayOutbox(OutboxPg(pool), mensajeria.publicador()).ejecutar_una_vez() == 1

    recibidos = mensajeria.recibir(mensajeria.aceptaciones, 2)
    assert _event_ids(recibidos) == [str(event_id), str(event_id)]
    assert recibidos[0]["Body"] == recibidos[1]["Body"]
    assert filas(limpia)[event_id][0] is not None


@pytest.mark.anyio
async def test_con_sns_inalcanzable_no_suma_intentos_y_publica_al_volver(
    limpia: str, mensajeria: Mensajeria
) -> None:
    ids = insertar(limpia, ["OfertaAceptada", "RecaudoConfirmado"])
    inalcanzable = cliente_sns(
        REGION,
        endpoint_url="http://127.0.0.1:9",
        timeout_conexion_s=0.5,
        timeout_lectura_s=0.5,
        intentos=1,
    )

    async with abrir_pool(limpia) as pool:
        caido = RelayOutbox(
            OutboxPg(pool), PublicadorSns(inalcanzable, mensajeria.topic_arn), max_intentos=2
        )
        resultados = [await caido.ejecutar_una_vez() for _ in range(3)]
        for event_id in ids:
            published_at, attempts, last_error = filas(limpia)[event_id]
            # Un fallo general no consume max_intentos: tres ciclos con max_intentos=2.
            assert (published_at, attempts) == (None, 0)
            assert last_error in {"EndpointConnectionError", "ConnectTimeoutError"}

        repuesto = RelayOutbox(OutboxPg(pool), mensajeria.publicador(), max_intentos=2)
        assert await repuesto.ejecutar_una_vez() == 2

    assert resultados == [0, 0, 0]
    assert all(published_at is not None for published_at, _, _ in filas(limpia).values())
    assert _event_ids(mensajeria.recibir(mensajeria.aceptaciones, 1)) == [str(ids[0])]
    assert _event_ids(mensajeria.recibir(mensajeria.recaudos, 1)) == [str(ids[1])]


@pytest.mark.anyio
async def test_un_mensaje_por_encima_del_limite_de_sns_suma_intentos(
    limpia: str, mensajeria: Mensajeria
) -> None:
    """4xx real de LocalStack propio de la fila: `InvalidParameter` (400) por superar 256 KiB.

    El estado HTTP sale de la respuesta real de botocore (`ResponseMetadata`).
    """
    grande, normal = insertar(limpia, ["OfertaAceptada", "OfertaAceptada"])
    with psycopg.connect(limpia) as conexion:
        conexion.execute(
            "UPDATE outbox SET payload = payload || jsonb_build_object('relleno', repeat('a', "
            "270000)) WHERE event_id = %s",
            (grande,),
        )

    async with abrir_pool(limpia) as pool:
        relay = RelayOutbox(OutboxPg(pool), mensajeria.publicador())
        assert await relay.ejecutar_una_vez() == 1
        assert filas(limpia)[grande][1] == 1
        # Con max_intentos por defecto (5), el sexto ciclo ya no la lee.
        assert [await relay.ejecutar_una_vez() for _ in range(5)] == [0] * 5

    published_at, attempts, last_error = filas(limpia)[grande]
    assert (published_at, attempts) == (None, 5)
    assert last_error == "InvalidParameterException: InvalidParameter"
    assert filas(limpia)[normal][0] is not None
    assert _event_ids(mensajeria.recibir(mensajeria.aceptaciones, 1)) == [str(normal)]


@pytest.mark.anyio
async def test_un_topico_inexistente_no_suma_intentos_y_publica_cuando_existe(
    limpia: str, mensajeria: Mensajeria
) -> None:
    """`NotFound` (404) real de LocalStack: describe el tópico, no la fila."""
    (event_id,) = insertar(limpia, ["OfertaAceptada"])
    nombre = f"tardio-{uuid4().hex[:8]}"
    arn = f"{mensajeria.topic_arn.rsplit(':', 1)[0]}:{nombre}"

    async with abrir_pool(limpia) as pool:
        relay = RelayOutbox(OutboxPg(pool), PublicadorSns(mensajeria.sns, arn))
        # Más ciclos que max_intentos (5): ninguno la consume.
        assert [await relay.ejecutar_una_vez() for _ in range(6)] == [0] * 6
        assert filas(limpia)[event_id] == (None, 0, "NotFoundException: NotFound")

        assert mensajeria.sns.create_topic(Name=nombre)["TopicArn"] == arn
        assert await relay.ejecutar_una_vez() == 1

    assert filas(limpia)[event_id][0] is not None


@pytest.mark.anyio
async def test_dos_relays_concurrentes_publican_cada_evento_una_sola_vez(
    limpia: str, mensajeria: Mensajeria
) -> None:
    ids = insertar(limpia, ["OfertaAceptada"] * 100)

    async def drenar(relay: RelayOutbox) -> int:
        total = 0
        while publicados := await relay.ejecutar_una_vez():
            total += publicados
        return total

    async with abrir_pool(limpia) as pool_a, abrir_pool(limpia) as pool_b:
        relay_a = RelayOutbox(OutboxPg(pool_a), mensajeria.publicador(), lote=10)
        relay_b = RelayOutbox(OutboxPg(pool_b), mensajeria.publicador(), lote=10)
        totales = await asyncio.gather(drenar(relay_a), drenar(relay_b))

    assert sum(totales) == 100
    assert all(total > 0 for total in totales), totales
    recibidos = _event_ids(mensajeria.recibir(mensajeria.aceptaciones, 100))
    assert sorted(recibidos) == sorted(str(i) for i in ids)
    assert mensajeria.vacia(mensajeria.aceptaciones)
