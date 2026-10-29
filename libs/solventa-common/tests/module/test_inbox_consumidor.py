"""Consumidor con inbox (HU-W16, 16.5) contra PostgreSQL y LocalStack reales (Testcontainers).

Cada prueba crea su cola con DLQ (`maxReceiveCount` = 5, como `infra/`) y una visibilidad corta. La
tabla `inbox` se crea con el DDL recomendado del docstring de `solventa_common.inbox`. El manejador
escribe su efecto en `efectos`, sin restricción de unicidad: si el efecto se repitiera, se vería.

Casos: el mismo `eventId` dos veces, un manejador que falla una vez, un mensaje envenenado que acaba
en la DLQ, dos consumidores concurrentes con el duplicado a la vez, el modo (b) con caída entre el
commit y la continuación, y de punta a punta con el relay de la 16.4 (outbox → SNS → cola filtrada
por `OfertaAceptada` → consumidor → inbox).

psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: las pruebas corren en un
SelectorEventLoop (en Linux es el bucle por defecto), como `test_outbox_relay.py`.
"""

import asyncio
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from solventa_common import inbox
from solventa_common.inbox import (
    ConsumidorInbox,
    Continuacion,
    EventoInvalido,
    EventoRecibido,
    InboxPg,
    ReceptorSqs,
    TransaccionInboxPg,
    cliente_sqs,
)
from solventa_common.outbox import OutboxPg, PublicadorSns, RelayOutbox, cliente_sns

docker = pytest.importorskip("docker")

IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
REGION = "us-east-1"
EVENTOS = Path(__file__).resolve().parents[4] / "contracts" / "events"
ESPERA_LARGA_S = 1
MAX_RECEPCIONES = 5

DDL_EFECTOS = """
CREATE TABLE IF NOT EXISTS efectos (
  id bigserial PRIMARY KEY,
  event_id uuid NOT NULL,
  etapa text NOT NULL
);
"""

# Copiado de services/polizas/src/polizas/adapters/outbound/migraciones/001_suscripcion.sql, como
# en test_outbox_relay.py: el recorrido de punta a punta parte de la forma real de la fila.
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

# Filtro de infra/platform/catalog.tf (dinero-aceptaciones).
FILTRO_ACEPTACIONES = {"eventType": ["OfertaAceptada"]}


def ddl_inbox() -> str:
    """El DDL recomendado, tal como está en el docstring del módulo."""
    bloque = re.search(r"```sql\n(.*?)```", inbox.__doc__ or "", re.DOTALL)
    assert bloque is not None, "el docstring de inbox.py no trae el bloque ```sql"
    return bloque.group(1)


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
            conexion.execute(ddl_inbox())
            conexion.execute(DDL_EFECTOS)
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
        conexion.execute("TRUNCATE inbox, efectos, outbox")
    return base


@pytest.fixture
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    # LocalStack acepta cualquier credencial; boto3 usa la cadena por defecto.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@dataclass
class Cola:
    """Una cola nueva con su DLQ (`maxReceiveCount` = 5)."""

    sqs: Any
    url: str
    dlq: str
    arn: str

    def enviar(self, cuerpo: str, tipo: str | None = "OfertaAceptada") -> None:
        atributos = (
            {} if tipo is None else {"eventType": {"DataType": "String", "StringValue": tipo}}
        )
        self.sqs.send_message(QueueUrl=self.url, MessageBody=cuerpo, MessageAttributes=atributos)

    def mensajes(self, url: str) -> int:
        """Visibles más en vuelo: 0 si todo se borró."""
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return sum(int(valor) for valor in atributos.values())

    def receptor(self, **kwargs: Any) -> ReceptorSqs:
        return ReceptorSqs(self.sqs, self.url, espera_larga_s=ESPERA_LARGA_S, **kwargs)


@pytest.fixture
def nueva_cola(localstack: str, credenciales: None) -> Callable[..., Cola]:
    sqs = cliente_sqs(REGION, endpoint_url=localstack, espera_larga_s=ESPERA_LARGA_S)

    def crear(visibilidad_s: int = 1) -> Cola:
        sufijo = uuid4().hex[:8]
        dlq = sqs.create_queue(QueueName=f"cola-{sufijo}-dlq")["QueueUrl"]
        dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        url = sqs.create_queue(
            QueueName=f"cola-{sufijo}",
            Attributes={
                "VisibilityTimeout": str(visibilidad_s),
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEPCIONES}
                ),
            },
        )["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        return Cola(sqs=sqs, url=url, dlq=dlq, arn=arn)

    return crear


@asynccontextmanager
async def abrir_pool(url: str) -> AsyncIterator[AsyncConnectionPool[Any]]:
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


# --- Manejador y datos -----------------------------------------------------------------------------
@dataclass
class ManejadorPg:
    """Escribe el efecto con la conexión de la transacción del inbox (modo a, o b con continuación)."""

    continuacion: bool = False
    fallos_del_manejador: int = 0
    fallos_de_la_continuacion: int = 0
    pausa_s: float = 0
    validar_esquema: bool = False
    eventos: list[EventoRecibido] = field(default_factory=list)
    continuaciones: int = 0
    marcas: list[tuple[str, float]] = field(default_factory=list)

    async def __call__(self, evento: EventoRecibido, tx: TransaccionInboxPg) -> Continuacion | None:
        self.eventos.append(evento)
        if self.validar_esquema and not _valida(evento.cuerpo):
            raise EventoInvalido
        await tx.conexion.execute(
            "INSERT INTO efectos (event_id, etapa) VALUES (%s, 'transaccion')", (evento.event_id,)
        )
        if self.pausa_s:
            self.marcas.append(("manejador", time.monotonic()))
            await asyncio.sleep(self.pausa_s)
            self.marcas.append(("fin manejador", time.monotonic()))
        if self.fallos_del_manejador:
            self.fallos_del_manejador -= 1
            raise RuntimeError("falla transitoria")
        if not self.continuacion:
            return None

        async def continuar() -> None:
            self.continuaciones += 1
            if self.fallos_de_la_continuacion:
                self.fallos_de_la_continuacion -= 1
                # Caída entre el commit y el efecto externo.
                raise ConnectionError("el proceso cae antes del efecto externo")

        return continuar


def _valida(cuerpo: str) -> bool:
    """Como hará el manejador de cada servicio: el cuerpo contra el esquema de su evento."""
    sobre = json.loads(cuerpo)
    esquema = json.loads((EVENTOS / f"{sobre['type']}.v1.schema.json").read_text(encoding="utf-8"))
    validador = Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    return bool(validador.is_valid(sobre))


def _payload_oferta() -> dict[str, Any]:
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


def _sobre(event_id: UUID | None = None) -> str:
    return json.dumps(
        {
            "eventId": str(event_id or uuid4()),
            "aggregateId": str(uuid4()),
            "type": "OfertaAceptada",
            "version": 1,
            "occurredAt": "2026-10-28T13:05:26Z",
            "correlationId": "corr-sintetico-01",
            "payload": _payload_oferta(),
        }
    )


def efectos(url: str) -> dict[UUID, list[str]]:
    with psycopg.connect(url) as conexion:
        resultado: dict[UUID, list[str]] = {}
        for event_id, etapa in conexion.execute("SELECT event_id, etapa FROM efectos ORDER BY id"):
            resultado.setdefault(event_id, []).append(etapa)
        return resultado


def filas_inbox(url: str) -> dict[UUID, str]:
    with psycopg.connect(url) as conexion:
        return dict(conexion.execute("SELECT event_id, event_type FROM inbox").fetchall())


async def drenar(
    consumidor: ConsumidorInbox[Any], procesados: int, limite_s: float = 30
) -> list[int]:
    """Ciclos hasta procesar `procesados` mensajes; devuelve lo que devolvió cada ciclo."""
    ciclos: list[int] = []
    limite = time.monotonic() + limite_s
    while sum(ciclos) < procesados:
        assert time.monotonic() < limite, f"sin procesar a tiempo: {ciclos}"
        ciclos.append(await consumidor.ejecutar_una_vez())
    return ciclos


# --- Casos -----------------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_el_mismo_event_id_enviado_dos_veces_tiene_efecto_una_sola_vez(
    limpia: str, nueva_cola: Callable[..., Cola]
) -> None:
    cola = nueva_cola()
    event_id = uuid4()
    cuerpo = _sobre(event_id)
    cola.enviar(cuerpo)
    cola.enviar(cuerpo)
    manejador = ManejadorPg()

    async with abrir_pool(limpia) as pool:
        await drenar(ConsumidorInbox(cola.receptor(), InboxPg(pool), manejador), 2)

    assert efectos(limpia) == {event_id: ["transaccion"]}
    assert filas_inbox(limpia) == {event_id: "OfertaAceptada"}
    assert [e.event_id for e in manejador.eventos] == [event_id]
    assert cola.mensajes(cola.url) == 0
    assert cola.mensajes(cola.dlq) == 0


@pytest.mark.anyio
async def test_un_manejador_que_falla_una_vez_se_reentrega_y_luego_tiene_exito(
    limpia: str, nueva_cola: Callable[..., Cola]
) -> None:
    cola = nueva_cola()
    event_id = uuid4()
    cola.enviar(_sobre(event_id))
    manejador = ManejadorPg(fallos_del_manejador=1)

    async with abrir_pool(limpia) as pool:
        ciclos = await drenar(ConsumidorInbox(cola.receptor(), InboxPg(pool), manejador), 1)

    # El primer intento escribió su efecto, pero el rollback lo deshizo junto con la fila del inbox.
    assert ciclos[0] == 0
    assert [e.recepciones for e in manejador.eventos] == [1, 2]
    assert efectos(limpia) == {event_id: ["transaccion"]}
    assert filas_inbox(limpia) == {event_id: "OfertaAceptada"}
    assert cola.mensajes(cola.url) == 0


@pytest.mark.anyio
async def test_un_mensaje_envenenado_llega_a_la_dlq_tras_5_recepciones(
    limpia: str, nueva_cola: Callable[..., Cola], telemetria: Any
) -> None:
    cola = nueva_cola()
    cuerpo = '{"eventId": "no-es-uuid", "type": "OfertaAceptada", "actorId": "Juan Pérez"}'
    cola.enviar(cuerpo)
    manejador = ManejadorPg()

    async with abrir_pool(limpia) as pool:
        consumidor = ConsumidorInbox(cola.receptor(), InboxPg(pool), manejador)
        limite = time.monotonic() + 40
        while cola.mensajes(cola.dlq) == 0:
            assert time.monotonic() < limite, "el mensaje no llegó a la DLQ"
            assert await consumidor.ejecutar_una_vez() == 0

    assert manejador.eventos == []
    assert filas_inbox(limpia) == {}
    envenenados = [r for r in telemetria.logs() if r.get("outcome") == "envenenado"]
    assert [r["receive_count"] for r in envenenados] == [1, 2, 3, 4, 5]
    assert {r["reason"] for r in envenenados} == {inbox.MOTIVO_EVENT_ID}
    assert "Juan Pérez" not in telemetria.texto_logs()
    assert cola.mensajes(cola.url) == 0
    (en_dlq,) = cola.sqs.receive_message(QueueUrl=cola.dlq, WaitTimeSeconds=1)["Messages"]
    assert en_dlq["Body"] == cuerpo


@dataclass
class InboxCronometrado:
    """`InboxPg` real; anota cuándo empieza y termina cada `registrar`."""

    real: InboxPg
    marcas: list[tuple[str, float]]

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator["TransaccionCronometrada"]:
        async with self.real.transaccion() as tx:
            yield TransaccionCronometrada(tx.conexion, tx.tabla, self.marcas)


@dataclass(frozen=True)
class TransaccionCronometrada(TransaccionInboxPg):
    marcas: list[tuple[str, float]]

    async def registrar(self, event_id: UUID, event_type: str, recibido_en: datetime) -> bool:
        self.marcas.append(("registrar", time.monotonic()))
        nuevo = await super().registrar(event_id, event_type, recibido_en)
        self.marcas.append(("nuevo" if nuevo else "duplicado", time.monotonic()))
        return nuevo


@pytest.mark.anyio
async def test_dos_consumidores_concurrentes_con_el_duplicado_a_la_vez_tienen_efecto_una_vez(
    limpia: str, nueva_cola: Callable[..., Cola]
) -> None:
    cola = nueva_cola(visibilidad_s=10)
    event_id = uuid4()
    cuerpo = _sobre(event_id)
    cola.enviar(cuerpo)
    cola.enviar(cuerpo)
    manejador = ManejadorPg(pausa_s=1)
    marcas: list[tuple[str, float]] = []

    async with abrir_pool(limpia) as pool_a, abrir_pool(limpia) as pool_b:
        consumidores = [
            ConsumidorInbox(
                cola.receptor(max_mensajes=1), InboxCronometrado(InboxPg(pool), marcas), manejador
            )
            for pool in (pool_a, pool_b)
        ]
        resultados = await asyncio.gather(*(c.ejecutar_una_vez() for c in consumidores))

    # Cada consumidor recibió una copia y la dio por procesada: uno como nuevo, otro como duplicado.
    assert resultados == [1, 1]
    assert [e.event_id for e in manejador.eventos] == [event_id]
    assert efectos(limpia) == {event_id: ["transaccion"]}
    assert filas_inbox(limpia) == {event_id: "OfertaAceptada"}
    # A la vez: los dos `registrar` empezaron antes de que el manejador terminara, y el duplicado
    # se resolvió solo después (esperó al commit del primero por la clave primaria).
    instantes = dict(manejador.marcas)
    inicios = [t for nombre, t in marcas if nombre == "registrar"]
    (duplicado,) = [t for nombre, t in marcas if nombre == "duplicado"]
    assert len(inicios) == 2
    assert max(inicios) < instantes["fin manejador"] <= duplicado
    assert cola.mensajes(cola.url) == 0


@pytest.mark.anyio
async def test_modo_b_con_caida_entre_el_commit_y_la_continuacion_no_repite_el_efecto(
    limpia: str, nueva_cola: Callable[..., Cola]
) -> None:
    cola = nueva_cola()
    event_id = uuid4()
    cola.enviar(_sobre(event_id))
    manejador = ManejadorPg(continuacion=True, fallos_de_la_continuacion=1)

    async with abrir_pool(limpia) as pool:
        consumidor = ConsumidorInbox(cola.receptor(), InboxPg(pool), manejador)
        assert await consumidor.ejecutar_una_vez() == 0
        # Tras el commit: la parte transaccional quedó y el mensaje sigue en la cola.
        assert efectos(limpia) == {event_id: ["transaccion"]}
        assert filas_inbox(limpia) == {event_id: "OfertaAceptada"}
        assert cola.mensajes(cola.url) == 1

        await drenar(consumidor, 1)

    # La reentrega fue un duplicado: ni otro efecto transaccional ni otra continuación.
    assert efectos(limpia) == {event_id: ["transaccion"]}
    assert len(manejador.eventos) == 1
    assert manejador.continuaciones == 1
    assert cola.mensajes(cola.url) == 0
    assert cola.mensajes(cola.dlq) == 0


@pytest.mark.anyio
async def test_de_punta_a_punta_del_outbox_al_inbox_por_sns_con_el_filtro_de_la_cola(
    limpia: str, localstack: str, nueva_cola: Callable[..., Cola], telemetria: Any
) -> None:
    cola = nueva_cola()
    sns = cliente_sns(REGION, endpoint_url=localstack)
    topic_arn = sns.create_topic(Name=f"eventos-{uuid4().hex[:8]}")["TopicArn"]
    sns.subscribe(
        TopicArn=topic_arn,
        Protocol="sqs",
        Endpoint=cola.arn,
        Attributes={
            "RawMessageDelivery": "true",
            "FilterPolicy": json.dumps(FILTRO_ACEPTACIONES),
        },
    )
    oferta, decision = uuid4(), uuid4()
    with psycopg.connect(limpia) as conexion:
        for event_id, tipo, payload in (
            (oferta, "OfertaAceptada", _payload_oferta()),
            (decision, "DecisionSuscripcionRegistrada", {"decisionId": str(uuid4())}),
        ):
            conexion.execute(
                "INSERT INTO outbox (event_id, aggregate_id, event_type, event_version, "
                "occurred_at, correlation_id, payload) VALUES (%s, %s, %s, 1, %s, %s, %s)",
                (event_id, uuid4(), tipo, datetime.now(UTC), "corr-e2e-01", Jsonb(payload)),
            )
    manejador = ManejadorPg(validar_esquema=True)

    async with abrir_pool(limpia) as pool:
        assert (
            await RelayOutbox(OutboxPg(pool), PublicadorSns(sns, topic_arn)).ejecutar_una_vez() == 2
        )
        await drenar(ConsumidorInbox(cola.receptor(), InboxPg(pool), manejador), 1)

    (evento,) = manejador.eventos
    assert (evento.event_id, evento.event_type, evento.correlation_id) == (
        oferta,
        "OfertaAceptada",
        "corr-e2e-01",
    )
    # El monto llega como texto decimal, sin pasar por float.
    assert evento.sobre["payload"]["prima"]["valor"] == "1850000.50"
    assert filas_inbox(limpia) == {oferta: "OfertaAceptada"}
    assert efectos(limpia) == {oferta: ["transaccion"]}
    registros = [r for r in telemetria.logs() if r.get("operation") == inbox.OPERACION]
    assert [(r["outcome"], r["correlation_id"]) for r in registros] == [("nuevo", "corr-e2e-01")]
    assert not [r for r in telemetria.logs() if r.get("message") == inbox.MENSAJE_TIPO_DISTINTO]
    # `DecisionSuscripcionRegistrada` no pasa el filtro de la cola.
    assert cola.mensajes(cola.url) == 0
