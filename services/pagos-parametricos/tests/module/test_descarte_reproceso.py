"""HU-W24 (N2): reproceso desde la DLQ de `parametricos-eventos` sin efecto duplicado, contra
PostgreSQL y SQS reales (Testcontainers + LocalStack).

La cola tiene DLQ (`maxReceiveCount` = 5, `catalog.tf`). El consumidor es el común
(`solventa_common.inbox.ConsumidorInbox`) con `RegistroFallosPg.al_fallar`, y un manejador de
prueba que exige `precipitacionMm` numérico y "paga" con una clave única por `eventId` (el manejador
real es de HU-W21). La cola de descarte usa `DlqSqs` y `ResolucionesPg` reales.

`TESTCONTAINERS_RYUK_DISABLED=true` en macOS.
"""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from jsonschema import Draft202012Validator
from pagos_parametricos.adapters.outbound.descartes_pg import (
    RegistroFallosPg,
    ResolucionesPg,
    migrar,
)
from pagos_parametricos.adapters.outbound.dlq_sqs import DlqSqs
from pagos_parametricos.adapters.outbound.reloj import RelojSistema
from pagos_parametricos.application.descartes import ColaDescarte
from pagos_parametricos.domain.descarte import Accion, Causa
from psycopg_pool import AsyncConnectionPool
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

docker = pytest.importorskip("docker")

IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
REGION = "us-east-1"
ESPERA_LARGA_S = 1
MAX_RECEPCIONES = 5
EVENTOS = Path(__file__).resolve().parents[4] / "contracts" / "events"

# Lo que HU-W21 crea en sus migraciones; aquí, lo mínimo para el manejador de prueba.
DDL_PRUEBA = """
CREATE TABLE IF NOT EXISTS inbox (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,
  recibido_en timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS pagos_prueba (
  event_id uuid PRIMARY KEY,
  precipitacion numeric NOT NULL
);
"""


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
        with psycopg.connect(url, autocommit=True) as conexion:
            conexion.execute(DDL_PRUEBA)
        yield url


@pytest.fixture(scope="module")
def localstack() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.localstack import LocalStackContainer

    with LocalStackContainer(image=IMAGEN_LOCALSTACK, region_name=REGION).with_services(
        "sqs"
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
        conexion.execute(
            "TRUNCATE inbox, pagos_prueba, fallos_consumo, resoluciones_descarte, outbox"
        )
    return base


@dataclass
class Colas:
    sqs: Any
    url: str

    @property
    def dlq(self) -> str:
        return f"{self.url}-dlq"

    def enviar(self, cuerpo: str, url: str | None = None) -> None:
        self.sqs.send_message(
            QueueUrl=url or self.url,
            MessageBody=cuerpo,
            MessageAttributes={
                "eventType": {"DataType": "String", "StringValue": "EventoParametricoObservado"}
            },
        )

    def contar(self, url: str | None = None) -> int:
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=url or self.url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return sum(int(v) for v in atributos.values())


@pytest.fixture
def colas(localstack: str, credenciales: None) -> Colas:
    sqs = cliente_sqs(REGION, endpoint_url=localstack, espera_larga_s=ESPERA_LARGA_S)
    nombre = f"parametricos-eventos-{uuid4().hex[:8]}"
    dlq = sqs.create_queue(
        QueueName=f"{nombre}-dlq", Attributes={"MessageRetentionPeriod": "1209600"}
    )["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    url = sqs.create_queue(
        QueueName=nombre,
        Attributes={
            "VisibilityTimeout": "1",
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEPCIONES}
            ),
        },
    )["QueueUrl"]
    return Colas(sqs=sqs, url=url)


@asynccontextmanager
async def abrir_pool(url: str) -> AsyncIterator[AsyncConnectionPool[Any]]:
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def sobre(precipitacion: Any, event_id: UUID | None = None) -> str:
    return json.dumps(
        {
            "eventId": str(event_id or uuid4()),
            "aggregateId": str(uuid4()),
            "type": "EventoParametricoObservado",
            "version": 1,
            "occurredAt": "2026-09-13T14:06:12Z",
            "correlationId": "corr-w24-r5",
            "payload": {
                "evento": "lluvia_acumulada",
                "numeroPoliza": "500-2026-000411",
                "precipitacionMm": precipitacion,
            },
        }
    )


async def pagar(evento: EventoRecibido, tx: TransaccionInboxPg) -> Continuacion | None:
    """Como el de HU-W21, reducido: un texto en lugar de número no valida; el pago es único."""
    valor = evento.sobre["payload"]["precipitacionMm"]
    if isinstance(valor, str | bool):
        raise EventoInvalido
    await tx.conexion.execute(
        "INSERT INTO pagos_prueba (event_id, precipitacion) VALUES (%s, %s) "
        "ON CONFLICT (event_id) DO NOTHING",
        (evento.event_id, valor),
    )
    return None


async def drenar(
    consumidor: ConsumidorInbox[Any], colas: Colas, *, dlq: int, limite_s: float = 40
) -> None:
    """Corre el consumidor hasta que la cola esté vacía y la DLQ tenga `dlq` mensajes."""
    parada = asyncio.Event()
    tarea = asyncio.create_task(consumidor.ejecutar(parada))
    limite = time.monotonic() + limite_s
    try:
        while colas.contar() or colas.contar(colas.dlq) != dlq:
            assert time.monotonic() < limite, "no se drenó a tiempo"
            await asyncio.sleep(0.2)
    finally:
        parada.set()
        await tarea


def filas(url: str, sentencia: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conexion:
        return list(conexion.execute(sentencia).fetchall())


def esquema_valido(sobre_outbox: dict[str, Any]) -> bool:
    esquema = json.loads((EVENTOS / "DescarteParametricoResuelto.v1.schema.json").read_text())
    return bool(Draft202012Validator(esquema).is_valid(sobre_outbox))


@pytest.mark.anyio
async def test_corregir_y_reprocesar_paga_una_vez_y_queda_auditado(
    limpia: str, colas: Colas
) -> None:
    evento_id = uuid4()
    colas.enviar(sobre("142,7", evento_id))

    async with abrir_pool(limpia) as pool:
        reloj = RelojSistema()
        fallos = RegistroFallosPg(pool, reloj)
        consumidor = ConsumidorInbox(
            ReceptorSqs(colas.sqs, colas.url, espera_larga_s=ESPERA_LARGA_S, max_mensajes=10),
            InboxPg(pool),
            pagar,
            concurrencia=4,
            al_fallar=fallos.al_fallar,
        )
        # AC3: tras 5 recepciones, a la DLQ, con su causa por MessageId.
        await drenar(consumidor, colas, dlq=1)
        cola = ColaDescarte(DlqSqs(colas.sqs, colas.url), fallos, ResolucionesPg(pool), reloj)

        listado = await cola.listar()
        (mensaje,) = listado.mensajes
        assert (mensaje.causa, mensaje.detalle, mensaje.intentos) == (
            Causa.FORMATO_INVALIDO,
            "esquema",
            MAX_RECEPCIONES,
        )
        assert (mensaje.evento_id, mensaje.poliza) == (str(evento_id), "500-2026-000411")
        assert listado.retencion_dias == 14
        assert colas.contar(colas.dlq) == 1  # listar no consume

        resolucion = await cola.corregir(
            mensaje.message_id, {"precipitacionMm": 142.7}, "sub-analista-01", "corr-h11"
        )
        assert resolucion.accion is Accion.CORREGIDO
        await drenar(consumidor, colas, dlq=0)

        # El mismo evento corregido vuelve a la DLQ por error (o lo reprocesan dos veces):
        # misma clave de idempotencia, ningún pago nuevo.
        assert resolucion.corregido is not None
        colas.enviar(resolucion.corregido, colas.dlq)
        (repetido,) = (await cola.listar()).mensajes
        await cola.reprocesar([repetido.message_id], "sub-analista-02", "corr-h10")
        await drenar(consumidor, colas, dlq=0)

    assert filas(limpia, "SELECT event_id, precipitacion::text FROM pagos_prueba") == [
        (evento_id, "142.7")
    ]
    eventos = filas(
        limpia,
        "SELECT event_id, aggregate_id, event_type, event_version, occurred_at, correlation_id, "
        "payload FROM outbox ORDER BY occurred_at",
    )
    assert [fila[2] for fila in eventos] == ["DescarteParametricoResuelto"] * 2
    corregido, reprocesado = (fila[6] for fila in eventos)
    assert (corregido["accion"], reprocesado["accion"]) == ("CORREGIDO", "REPROCESADO")
    # Original y corrección en la auditoría.
    assert json.loads(corregido["original"])["payload"]["precipitacionMm"] == "142,7"
    assert json.loads(corregido["corregido"])["payload"]["precipitacionMm"] == 142.7
    assert all(fila[1] == evento_id for fila in eventos)
    for fila in eventos:
        assert esquema_valido(
            {
                "eventId": str(fila[0]),
                "aggregateId": str(fila[1]),
                "type": fila[2],
                "version": fila[3],
                "occurredAt": fila[4].isoformat(),
                "correlationId": fila[5],
                "payload": fila[6],
            }
        )


@pytest.mark.anyio
async def test_descartar_lo_saca_de_la_dlq_sin_reenviar_y_lo_audita(
    limpia: str, colas: Colas
) -> None:
    colas.enviar("{no es un sobre", colas.dlq)
    colas.enviar(sobre("abc"), colas.dlq)

    async with abrir_pool(limpia) as pool:
        reloj = RelojSistema()
        cola = ColaDescarte(
            DlqSqs(colas.sqs, colas.url), RegistroFallosPg(pool, reloj), ResolucionesPg(pool), reloj
        )
        mensajes = (await cola.listar()).mensajes
        assert len(mensajes) == 2
        for mensaje in mensajes:
            await cola.descartar(mensaje.message_id, "sub-analista-01", "corr-h11", "prueba")
        # Repetir es idempotente: la misma resolución.
        repetida = await cola.descartar(mensajes[0].message_id, "sub-analista-01", "corr-h11")

    assert repetida.accion is Accion.DESCARTADO
    assert colas.contar(colas.dlq) == 0
    assert colas.contar() == 0
    assert filas(limpia, "SELECT count(*) FROM outbox") == [(2,)]
    assert filas(limpia, "SELECT count(*) FROM resoluciones_descarte") == [(2,)]
