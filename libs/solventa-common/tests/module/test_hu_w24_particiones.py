"""HU-W24 (N2): particiones (SNS → cola por grupo de consumidores + DLQ) con contrapresión, contra
PostgreSQL y LocalStack reales (Testcontainers).

Cada prueba arma su tópico y sus colas como `infra/platform/messaging.tf`: suscripción con entrega en
crudo, filtro por `eventType`, DLQ con `maxReceiveCount` = 5 y visibilidad corta. La "proyección" de
prueba es una copia de coberturas con la regla de `solventa_common.versionado`; el manejador real de
`coberturas_activas` es de HU-W21.

Casos (AC1 a AC4): duplicados, desorden (cancelación antes de emisión), visibilidad vencida a mitad
del proceso, mensaje envenenado que termina en la DLQ con su causa, un grupo caído mientras otro
procesa, contrapresión con varias tareas y parada que libera lo recibido.

`TESTCONTAINERS_RYUK_DISABLED=true` en macOS (lo fija `_docker_disponible`).
"""

import asyncio
import json
import os
import random
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool
from solventa_common import inbox
from solventa_common.inbox import (
    ConsumidorInbox,
    Continuacion,
    Desenlace,
    EventoRecibido,
    InboxPg,
    MensajeSqs,
    ReceptorSqs,
    TransaccionInboxPg,
    cliente_sqs,
)
from solventa_common.outbox import cliente_sns
from solventa_common.versionado import aplicar_por_version, leer_version, upsert_por_version

docker = pytest.importorskip("docker")

IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
REGION = "us-east-1"
ESPERA_LARGA_S = 1
MAX_RECEPCIONES = 5

DDL_COBERTURAS = """
CREATE TABLE IF NOT EXISTS coberturas (
  poliza_id uuid PRIMARY KEY,
  estado text NOT NULL,
  aggregate_version int NOT NULL
);
CREATE TABLE IF NOT EXISTS efectos (
  id bigserial PRIMARY KEY,
  event_id uuid NOT NULL,
  grupo text NOT NULL
);
CREATE TABLE IF NOT EXISTS fallos (
  message_id text PRIMARY KEY,
  event_id uuid NULL,
  outcome text NOT NULL,
  causa text NULL,
  recepciones int NULL
);
"""

UPSERT = upsert_por_version("coberturas", ["poliza_id"], ["estado"])
ESTADOS = {
    "PolizaEmitida": "ACTIVA",
    "PolizaSuspendida": "SUSPENDIDA",
    "PolizaCancelada": "CANCELADA",
}


def ddl_inbox() -> str:
    bloque = re.search(r"```sql\n(.*?)```", inbox.__doc__ or "", re.DOTALL)
    assert bloque is not None
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
            conexion.execute(DDL_COBERTURAS)
            # Un inbox por grupo: cada grupo de consumidores es un servicio con su base.
            conexion.execute(ddl_inbox().replace("inbox", "inbox_auditoria"))
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
        conexion.execute("TRUNCATE inbox, inbox_auditoria, coberturas, efectos, fallos")
    return base


@pytest.fixture
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@dataclass
class Cola:
    sqs: Any
    url: str
    dlq: str

    def receptor(self, **kwargs: Any) -> ReceptorSqs:
        return ReceptorSqs(self.sqs, self.url, espera_larga_s=ESPERA_LARGA_S, **kwargs)

    def contar(self, url: str | None = None) -> tuple[int, int]:
        """(visibles, en vuelo)."""
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=url or self.url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return (
            int(atributos["ApproximateNumberOfMessages"]),
            int(atributos["ApproximateNumberOfMessagesNotVisible"]),
        )

    def esperar_entrega(self, cuantos: int, limite_s: float = 15) -> None:
        """SNS entrega en segundo plano: espera a que la cola tenga lo publicado."""
        limite = time.monotonic() + limite_s
        while sum(self.contar()) < cuantos:
            assert time.monotonic() < limite, f"SNS no entregó {cuantos} mensajes a tiempo"
            time.sleep(0.05)

    def mensajes_dlq(self) -> list[dict[str, Any]]:
        respuesta = self.sqs.receive_message(
            QueueUrl=self.dlq,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=1,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        return list(respuesta.get("Messages", []))


class ReceptorContado(ReceptorSqs):
    """Anota cada `MessageId` recibido (las recepciones repetidas, también)."""

    recibidos: list[str]

    async def recibir_hasta(self, maximo: int) -> list[MensajeSqs]:
        mensajes = await super().recibir_hasta(maximo)
        self.recibidos.extend(m.message_id for m in mensajes)
        return mensajes


def contado(cola: Cola, recibidos: list[str]) -> ReceptorContado:
    receptor = ReceptorContado(cola.sqs, cola.url, espera_larga_s=ESPERA_LARGA_S)
    receptor.recibidos = recibidos
    return receptor


@dataclass
class Particiones:
    """Tópico de eventos de negocio con una cola por grupo de consumidores (messaging.tf)."""

    sns: Any
    sqs: Any
    topico: str

    def grupo(self, filtro: dict[str, Any] | None, visibilidad_s: int = 2) -> Cola:
        sufijo = uuid4().hex[:8]
        dlq = self.sqs.create_queue(QueueName=f"grupo-{sufijo}-dlq")["QueueUrl"]
        dlq_arn = self._arn(dlq)
        url = self.sqs.create_queue(
            QueueName=f"grupo-{sufijo}",
            Attributes={
                "VisibilityTimeout": str(visibilidad_s),
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEPCIONES}
                ),
            },
        )["QueueUrl"]
        atributos: dict[str, str] = {"RawMessageDelivery": "true"}
        if filtro is not None:
            atributos["FilterPolicy"] = json.dumps(filtro)
        self.sns.subscribe(
            TopicArn=self.topico, Protocol="sqs", Endpoint=self._arn(url), Attributes=atributos
        )
        return Cola(sqs=self.sqs, url=url, dlq=dlq)

    def publicar(self, cuerpo: str, tipo: str) -> None:
        self.sns.publish(
            TopicArn=self.topico,
            Message=cuerpo,
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": tipo}},
        )

    def _arn(self, url: str) -> str:
        return str(
            self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
                "QueueArn"
            ]
        )


@pytest.fixture
def particiones(localstack: str, credenciales: None) -> Particiones:
    sqs = cliente_sqs(REGION, endpoint_url=localstack, espera_larga_s=ESPERA_LARGA_S)
    sns = cliente_sns(REGION, endpoint_url=localstack)
    topico = sns.create_topic(Name=f"eventos-negocio-{uuid4().hex[:8]}")["TopicArn"]
    return Particiones(sns=sns, sqs=sqs, topico=topico)


@asynccontextmanager
async def abrir_pool(url: str, tamano: int = 4) -> AsyncIterator[AsyncConnectionPool[Any]]:
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        url, min_size=1, max_size=tamano, open=False
    )
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


# --- Eventos y manejadores ---------------------------------------------------------------------
def sobre(tipo: str, poliza: UUID, version: int, *, event_id: UUID | None = None) -> str:
    return json.dumps(
        {
            "eventId": str(event_id or uuid4()),
            "aggregateId": str(poliza),
            "aggregateVersion": version,
            "type": tipo,
            "version": 1,
            "occurredAt": "2026-11-05T14:06:12Z",
            "correlationId": f"corr-w24-{version}",
            "payload": {"polizaId": str(poliza)},
        }
    )


@dataclass
class ProyeccionCoberturas:
    """Modo (a): la copia de coberturas con la regla de versión, en la transacción del inbox."""

    pausa_s: float = 0
    fallar_si: Callable[[EventoRecibido], bool] = lambda _: False
    llamadas: list[UUID] = field(default_factory=list)
    activos: int = 0
    maximo_activos: int = 0

    async def __call__(self, evento: EventoRecibido, tx: TransaccionInboxPg) -> Continuacion | None:
        self.llamadas.append(evento.event_id)
        self.activos += 1
        self.maximo_activos = max(self.maximo_activos, self.activos)
        try:
            if self.fallar_si(evento):
                raise ValueError("precipitacion no numérica")
            version = leer_version(evento)
            await aplicar_por_version(
                tx.conexion,
                UPSERT,
                [UUID(evento.sobre["aggregateId"])],
                [ESTADOS[evento.event_type]],
                version,
            )
            await tx.conexion.execute(
                "INSERT INTO efectos (event_id, grupo) VALUES (%s, 'coberturas')",
                (evento.event_id,),
            )
            if self.pausa_s:
                await asyncio.sleep(self.pausa_s)
            return None
        finally:
            self.activos -= 1


@dataclass
class Auditoria:
    """Otro grupo: registra todo, en su propio inbox."""

    async def __call__(self, evento: EventoRecibido, tx: TransaccionInboxPg) -> Continuacion | None:
        await tx.conexion.execute(
            "INSERT INTO efectos (event_id, grupo) VALUES (%s, 'auditoria')", (evento.event_id,)
        )
        return None


def anotar_fallos(url: str) -> Callable[..., Any]:
    """Como `pagos-parametricos`: guarda la causa por `MessageId` para mostrarla en la DLQ."""

    async def al_fallar(
        mensaje: MensajeSqs, evento: EventoRecibido | None, desenlace: Desenlace
    ) -> None:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conexion:
            await conexion.execute(
                "INSERT INTO fallos (message_id, event_id, outcome, causa, recepciones) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (message_id) DO UPDATE SET "
                "outcome = EXCLUDED.outcome, causa = EXCLUDED.causa, "
                "recepciones = EXCLUDED.recepciones",
                (
                    mensaje.message_id,
                    evento.event_id if evento else None,
                    desenlace.outcome,
                    desenlace.error_type or desenlace.reason,
                    mensaje.recepciones,
                ),
            )

    return al_fallar


def consultar(url: str, sentencia: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conexion:
        return list(conexion.execute(sentencia).fetchall())


async def correr(
    consumidores: list[ConsumidorInbox[Any]],
    hasta: Callable[[], bool],
    limite_s: float = 40,
) -> None:
    """Corre los consumidores en bucle (varias tareas del mismo grupo) hasta la condición."""
    parada = asyncio.Event()
    tareas = [asyncio.create_task(c.ejecutar(parada)) for c in consumidores]
    limite = time.monotonic() + limite_s
    try:
        while not hasta():
            assert time.monotonic() < limite, "no se cumplió a tiempo"
            await asyncio.sleep(0.1)
    finally:
        parada.set()
        await asyncio.gather(*tareas)


FILTRO_POLIZAS = {"eventType": [{"prefix": "Poliza"}]}


# --- AC4: duplicados y desorden --------------------------------------------------------------
@pytest.mark.anyio
async def test_duplicados_en_varias_tareas_tienen_efecto_una_sola_vez(
    limpia: str, particiones: Particiones
) -> None:
    cola = particiones.grupo(FILTRO_POLIZAS)
    poliza, event_id = uuid4(), uuid4()
    for _ in range(4):
        particiones.publicar(sobre("PolizaEmitida", poliza, 1, event_id=event_id), "PolizaEmitida")
    cola.esperar_entrega(4)
    proyeccion = ProyeccionCoberturas(pausa_s=0.05)

    async with abrir_pool(limpia) as pool:
        tareas = [
            ConsumidorInbox(
                cola.receptor(max_mensajes=10), InboxPg(pool), proyeccion, concurrencia=4
            )
            for _ in range(2)
        ]
        await correr(tareas, lambda: cola.contar() == (0, 0))

    assert consultar(limpia, "SELECT count(*) FROM efectos") == [(1,)]
    assert consultar(limpia, "SELECT estado, aggregate_version FROM coberturas") == [("ACTIVA", 1)]


@pytest.mark.anyio
async def test_una_cancelacion_que_llega_antes_que_la_emision_deja_la_cobertura_cancelada(
    limpia: str, particiones: Particiones
) -> None:
    cola = particiones.grupo(FILTRO_POLIZAS)
    poliza = uuid4()
    particiones.publicar(sobre("PolizaCancelada", poliza, 2), "PolizaCancelada")
    cola.esperar_entrega(1)

    async with abrir_pool(limpia) as pool:
        consumidor = ConsumidorInbox(
            cola.receptor(max_mensajes=10), InboxPg(pool), ProyeccionCoberturas(), concurrencia=2
        )
        await correr([consumidor], lambda: cola.contar() == (0, 0))
        # La emisión atrasada llega después: se confirma, pero no reactiva la cobertura.
        particiones.publicar(sobre("PolizaEmitida", poliza, 1), "PolizaEmitida")
        cola.esperar_entrega(1)
        await correr([consumidor], lambda: cola.contar() == (0, 0))

    assert consultar(limpia, "SELECT estado, aggregate_version FROM coberturas") == [
        ("CANCELADA", 2)
    ]
    assert consultar(limpia, "SELECT count(*) FROM inbox") == [(2,)]


@pytest.mark.anyio
async def test_en_cualquier_orden_y_con_duplicados_el_estado_final_es_el_de_la_entrega_ordenada(
    limpia: str, particiones: Particiones
) -> None:
    cola = particiones.grupo(FILTRO_POLIZAS)
    polizas = [uuid4() for _ in range(8)]
    historia = [("PolizaEmitida", 1), ("PolizaSuspendida", 2), ("PolizaCancelada", 3)]
    entregas = [
        (sobre(tipo, poliza, version, event_id=uuid4()), tipo)
        for poliza in polizas
        for tipo, version in historia
    ]
    entregas += random.Random(24).sample(entregas, 8)  # duplicados por eventId
    random.Random(66).shuffle(entregas)
    for cuerpo, tipo in entregas:
        particiones.publicar(cuerpo, tipo)
    cola.esperar_entrega(len(entregas))

    async with abrir_pool(limpia) as pool:
        tareas = [
            ConsumidorInbox(
                cola.receptor(max_mensajes=10),
                InboxPg(pool),
                ProyeccionCoberturas(),
                concurrencia=5,
            )
            for _ in range(3)
        ]
        await correr(tareas, lambda: cola.contar() == (0, 0))

    assert sorted(
        consultar(limpia, "SELECT poliza_id, estado, aggregate_version FROM coberturas")
    ) == sorted((poliza, "CANCELADA", 3) for poliza in polizas)
    assert consultar(limpia, "SELECT count(*) FROM efectos") == [(len(polizas) * len(historia),)]


# --- Visibilidad -------------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sin_latido_la_visibilidad_vence_a_mitad_y_el_inbox_evita_el_doble_efecto(
    limpia: str, particiones: Particiones
) -> None:
    cola = particiones.grupo(FILTRO_POLIZAS, visibilidad_s=2)
    poliza = uuid4()
    particiones.publicar(sobre("PolizaEmitida", poliza, 1), "PolizaEmitida")
    cola.esperar_entrega(1)
    proyeccion = ProyeccionCoberturas(pausa_s=3.5)
    recibidos: list[str] = []

    async with abrir_pool(limpia) as pool:
        tareas = [
            ConsumidorInbox(contado(cola, recibidos), InboxPg(pool), proyeccion, concurrencia=1)
            for _ in range(2)
        ]
        await correr(tareas, lambda: cola.contar() == (0, 0))

    # El mensaje reapareció y otra tarea lo tomó mientras el primero seguía en curso. Su INSERT en
    # el inbox esperó el commit del primero y lo vio duplicado: un manejador, un efecto.
    assert len(recibidos) == 2 and len(set(recibidos)) == 1
    assert len(proyeccion.llamadas) == 1
    assert consultar(limpia, "SELECT count(*) FROM efectos") == [(1,)]


@pytest.mark.anyio
async def test_con_latido_un_proceso_largo_no_reaparece(
    limpia: str, particiones: Particiones
) -> None:
    cola = particiones.grupo(FILTRO_POLIZAS, visibilidad_s=2)
    particiones.publicar(sobre("PolizaEmitida", uuid4(), 1), "PolizaEmitida")
    cola.esperar_entrega(1)
    proyeccion = ProyeccionCoberturas(pausa_s=3.5)
    recibidos: list[str] = []

    async with abrir_pool(limpia) as pool:
        tareas = [
            ConsumidorInbox(
                contado(cola, recibidos), InboxPg(pool), proyeccion, concurrencia=1, visibilidad_s=2
            )
            for _ in range(2)
        ]
        await correr(tareas, lambda: cola.contar() == (0, 0))

    assert len(recibidos) == 1
    assert len(proyeccion.llamadas) == 1
    assert consultar(limpia, "SELECT count(*) FROM efectos") == [(1,)]


# --- AC3: mensaje envenenado --------------------------------------------------------------------
@pytest.mark.anyio
async def test_un_envenenado_termina_en_la_dlq_con_su_causa_y_no_frena_a_los_demas(
    limpia: str, particiones: Particiones
) -> None:
    cola = particiones.grupo(FILTRO_POLIZAS, visibilidad_s=1)
    sanos = [uuid4() for _ in range(5)]
    for poliza in sanos:
        particiones.publicar(sobre("PolizaEmitida", poliza, 1), "PolizaEmitida")
    malo = uuid4()
    particiones.publicar(sobre("PolizaEmitida", malo, 1), "PolizaEmitida")
    cola.esperar_entrega(6)
    proyeccion = ProyeccionCoberturas(
        fallar_si=lambda evento: evento.sobre["aggregateId"] == str(malo)
    )

    async with abrir_pool(limpia) as pool:
        consumidor = ConsumidorInbox(
            cola.receptor(max_mensajes=10),
            InboxPg(pool),
            proyeccion,
            concurrencia=3,
            al_fallar=anotar_fallos(limpia),
        )
        await correr(
            [consumidor], lambda: cola.contar() == (0, 0) and cola.contar(cola.dlq)[0] == 1
        )

    assert {fila[0] for fila in consultar(limpia, "SELECT poliza_id FROM coberturas")} == set(sanos)
    (en_dlq,) = cola.mensajes_dlq()
    # Trazabilidad: el MessageId se conserva al pasar a la DLQ y su causa quedó anotada.
    ((outcome, causa, recepciones),) = consultar(
        limpia,
        f"SELECT outcome, causa, recepciones FROM fallos WHERE message_id = '{en_dlq['MessageId']}'",
    )
    assert (outcome, causa, recepciones) == (inbox.FALLO_TRANSACCION, "ValueError", MAX_RECEPCIONES)
    assert json.loads(en_dlq["Body"])["aggregateId"] == str(malo)


# --- AC1 y AC2: contrapresión y aislamiento -------------------------------------------------------
@pytest.mark.anyio
async def test_un_grupo_caido_no_frena_al_otro_y_su_cola_conserva_el_trabajo(
    limpia: str, particiones: Particiones
) -> None:
    coberturas = particiones.grupo(FILTRO_POLIZAS)
    auditoria = particiones.grupo(None)
    publicados = 20
    for _ in range(publicados):
        particiones.publicar(sobre("PolizaEmitida", uuid4(), 1), "PolizaEmitida")
    coberturas.esperar_entrega(publicados)
    auditoria.esperar_entrega(publicados)

    async with abrir_pool(limpia) as pool:
        # Solo corre auditoría: el grupo de coberturas está caído.
        await correr(
            [
                ConsumidorInbox(
                    auditoria.receptor(max_mensajes=10),
                    InboxPg(pool, "inbox_auditoria"),
                    Auditoria(),
                    concurrencia=5,
                )
            ],
            lambda: auditoria.contar() == (0, 0),
        )
        assert consultar(limpia, "SELECT grupo, count(*) FROM efectos GROUP BY grupo") == [
            ("auditoria", publicados)
        ]
        assert coberturas.contar() == (publicados, 0)

        # El grupo vuelve y drena lo retenido, sin depender del otro.
        await correr(
            [
                ConsumidorInbox(
                    coberturas.receptor(max_mensajes=10),
                    InboxPg(pool),
                    ProyeccionCoberturas(),
                    concurrencia=5,
                )
            ],
            lambda: coberturas.contar() == (0, 0),
        )
    assert sorted(consultar(limpia, "SELECT grupo, count(*) FROM efectos GROUP BY grupo")) == [
        ("auditoria", publicados),
        ("coberturas", publicados),
    ]
    assert coberturas.contar(coberturas.dlq) == (0, 0)


@pytest.mark.anyio
async def test_cada_tarea_recibe_solo_lo_que_puede_procesar(
    limpia: str, particiones: Particiones
) -> None:
    cola = particiones.grupo(FILTRO_POLIZAS, visibilidad_s=10)
    publicados = 30
    for _ in range(publicados):
        particiones.publicar(sobre("PolizaEmitida", uuid4(), 1), "PolizaEmitida")
    cola.esperar_entrega(publicados)
    proyeccion = ProyeccionCoberturas(pausa_s=0.2)
    en_vuelo: list[int] = []

    async with abrir_pool(limpia, tamano=8) as pool:
        tareas = [
            ConsumidorInbox(
                cola.receptor(max_mensajes=10), InboxPg(pool), proyeccion, concurrencia=3
            )
            for _ in range(2)
        ]

        def drenada() -> bool:
            visibles, vuelo = cola.contar()
            en_vuelo.append(vuelo)
            return (visibles, vuelo) == (0, 0)

        await correr(tareas, drenada)

    # Dos tareas de 3: nunca más de 6 recibidos a la vez; el resto lo retuvo la cola.
    assert proyeccion.maximo_activos <= 6
    assert max(en_vuelo) <= 6
    assert consultar(limpia, "SELECT count(*) FROM efectos") == [(publicados,)]


# --- Parada (SIGTERM) ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_la_parada_libera_lo_recibido_y_otra_tarea_lo_toma_sin_esperar_la_visibilidad(
    limpia: str, particiones: Particiones
) -> None:
    cola = particiones.grupo(FILTRO_POLIZAS, visibilidad_s=30)
    for _ in range(6):
        particiones.publicar(sobre("PolizaEmitida", uuid4(), 1), "PolizaEmitida")
    cola.esperar_entrega(6)
    bloqueo = asyncio.Event()

    async def lento(evento: EventoRecibido, tx: TransaccionInboxPg) -> Continuacion | None:
        await bloqueo.wait()
        return None

    async with abrir_pool(limpia) as pool:
        saliente = ConsumidorInbox(
            cola.receptor(max_mensajes=10), InboxPg(pool), lento, concurrencia=6, plazo_parada_s=0.2
        )
        parada = asyncio.Event()
        tarea = asyncio.create_task(saliente.ejecutar(parada))
        limite = time.monotonic() + 15
        while saliente.en_mano < 6:
            assert time.monotonic() < limite
            await asyncio.sleep(0.05)
        parada.set()
        await tarea
        inicio = time.monotonic()

        # Con 30 s de visibilidad, si no se liberaran, la otra tarea esperaría 30 s.
        await correr(
            [
                ConsumidorInbox(
                    cola.receptor(max_mensajes=10),
                    InboxPg(pool),
                    ProyeccionCoberturas(),
                    concurrencia=6,
                )
            ],
            lambda: cola.contar() == (0, 0),
            limite_s=10,
        )
    assert time.monotonic() - inicio < 10
    assert consultar(limpia, "SELECT count(*) FROM efectos") == [(6,)]
