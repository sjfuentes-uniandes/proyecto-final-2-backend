"""HU-W25 (paso 25.5, AC3): una tarea que se retira drena sus dos colas sin perder ni duplicar.

La app real (`crear_app`, su lifespan y `main._consumidores`) corre sobre PostgreSQL y LocalStack
SQS, con las colas a 30 s de visibilidad, como en `catalog.tf`. Los manejadores son los reales; un
envoltorio retiene `EN_CURSO` eventos y una póliza con una compuerta para que estén **en curso** al
cerrar: cada consumidor tiene algo que drenar.

1. Con lo retenido en curso, se cierra el lifespan (lo que hace uvicorn tras SIGTERM). **Los dos
   consumidores reciben la parada a la vez**: si pararan en serie, el de eventos seguiría recibiendo
   mientras el de pólizas espera a la suya.
2. Con la parada ya dada, llegan `TARDIOS` eventos y otra póliza: la espera larga que cada
   consumidor tenía abierta los recibe **sin empezarlos** y debe liberarlos (visibilidad 0).
3. Se abre la compuerta: lo que estaba en curso termina dentro del plazo y se borra.
4. Otra instancia (los consumidores de `armar_consumidores`) toma lo liberado sin esperar la
   visibilidad, y `eventos_evaluados` queda con exactamente `EN_CURSO + TARDIOS` filas.

Sin llamadas a `siniestros`: los eventos van a una zona sin coberturas. `TESTCONTAINERS_RYUK_DISABLED`
en macOS.
"""

import asyncio
import copy
import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from pagos_parametricos import main
from pagos_parametricos.adapters.outbound.descartes_pg import migrar
from pagos_parametricos.adapters.outbound.reloj import RelojSistema
from pagos_parametricos.config import Settings
from pagos_parametricos.main import armar_consumidores, crear_app
from psycopg_pool import AsyncConnectionPool
from solventa_common.inbox import MENSAJE_LIBERADO, ConsumidorInbox, EventoRecibido, cliente_sqs

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio

IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
REGION = "us-east-1"
VISIBILIDAD_COLA_S = 30
EN_CURSO = 3
TARDIOS = 5
ZONA_SIN_COBERTURAS = "W25SINPOLIZAS"  # `zona`: ^[A-Z0-9]{2,16}$
TABLAS = (
    "evaluaciones, activaciones, eventos_evaluados, coberturas_activas, inbox, fallos_consumo, "
    "resoluciones_descarte, outbox"
)


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


def sql(url: str, consulta: str, *parametros: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.execute(consulta, parametros)
        return cursor.fetchall() if cursor.description else []


async def esperar(condicion: Callable[[], Any], plazo_s: float, que: str) -> Any:
    """Repite `condicion` (en un hilo) hasta que dé un valor verdadero."""
    limite = time.monotonic() + plazo_s
    while True:
        valor = await asyncio.to_thread(condicion)
        if valor:
            return valor
        if time.monotonic() > limite:
            raise AssertionError(f"no se cumplió en {plazo_s} s: {que}")
        await asyncio.sleep(0.1)


@dataclass
class Colas:
    sqs: Any
    eventos: str
    polizas: str

    def enviar(self, url: str, sobre: dict[str, Any]) -> str:
        message_id: str = self.sqs.send_message(
            QueueUrl=url,
            MessageBody=json.dumps(sobre),
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": sobre["type"]}},
        )["MessageId"]
        return message_id

    def conteo(self, url: str) -> tuple[int, int]:
        """(visibles, en vuelo): un mensaje recibido y no borrado ni liberado cuenta en vuelo."""
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        return (
            int(atributos["ApproximateNumberOfMessages"]),
            int(atributos["ApproximateNumberOfMessagesNotVisible"]),
        )


def _crear_cola(sqs: Any, nombre: str) -> str:
    dlq = sqs.create_queue(QueueName=f"{nombre}-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    url: str = sqs.create_queue(
        QueueName=nombre,
        Attributes={
            "VisibilityTimeout": str(VISIBILIDAD_COLA_S),
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": 5}),
        },
    )["QueueUrl"]
    return url


@pytest.fixture
def colas(base: str, localstack: str, credenciales: None) -> Colas:
    sql(base, f"TRUNCATE {TABLAS}")
    sqs = cliente_sqs(REGION, endpoint_url=localstack)
    sufijo = uuid4().hex[:8]
    return Colas(
        sqs,
        _crear_cola(sqs, f"parametricos-eventos-w25-{sufijo}"),
        _crear_cola(sqs, f"parametricos-polizas-w25-{sufijo}"),
    )


class SiniestrosQueNoSeLlaman:
    """Los eventos van a una zona sin coberturas: no hay activaciones que llevar a `siniestros`."""

    async def crear(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no debía llamarse a siniestros")


@dataclass
class Compuerta:
    """Retiene en el manejador real los eventos `lentos` hasta que se abre."""

    lentos: set[UUID] = field(default_factory=set)
    en_curso: list[UUID] = field(default_factory=list)
    abierta: asyncio.Event = field(default_factory=asyncio.Event)


@pytest.fixture
def compuerta(monkeypatch: pytest.MonkeyPatch) -> Compuerta:
    retencion = Compuerta()

    def retener(manejar: Any) -> Any:
        async def manejador(evento: EventoRecibido, tx: Any) -> Any:
            if evento.event_id in retencion.lentos:
                retencion.en_curso.append(evento.event_id)
                await retencion.abierta.wait()
            return await manejar(evento, tx)

        return manejador

    eventos, polizas = main.manejador_eventos, main.manejador_polizas
    monkeypatch.setattr(main, "manejador_eventos", lambda *a: retener(eventos(*a)))
    monkeypatch.setattr(main, "manejador_polizas", lambda *a: retener(polizas(*a)))
    return retencion


@pytest.fixture
def consumidores_creados(monkeypatch: pytest.MonkeyPatch) -> list[ConsumidorInbox[Any]]:
    """Los `ConsumidorInbox` que arma la app, sin cambiar su comportamiento: para ver su parada."""
    creados: list[ConsumidorInbox[Any]] = []

    class Registrado(ConsumidorInbox[Any]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            creados.append(self)

    monkeypatch.setattr(main, "ConsumidorInbox", Registrado)
    return creados


def sobre_observado(
    observado: Callable[[dict[str, Any]], dict[str, Any]], caso: dict[str, Any], i: int
) -> dict[str, Any]:
    """Un `EventoParametricoObservado` sintético propio, en una zona sin coberturas."""
    sobre = copy.deepcopy(observado(caso))
    evento_id = str(uuid4())
    sobre["eventId"] = sobre["aggregateId"] = sobre["payload"]["eventoId"] = evento_id
    sobre["correlationId"] = f"corr-w25-drenaje-{i}"
    sobre["payload"]["idExterno"] = f"w25-drenaje-{i}"
    sobre["payload"]["zona"] = ZONA_SIN_COBERTURAS
    sobre["payload"]["archivoRef"] = f"telemetria/2026/11/10/{evento_id}.json"
    return sobre


async def test_al_cerrar_el_lifespan_lo_en_curso_termina_lo_recibido_se_libera_y_nada_se_repite(
    colas: Colas,
    base: str,
    compuerta: Compuerta,
    consumidores_creados: list[ConsumidorInbox[Any]],
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        SQS_PARAMETRICOS_EVENTOS_URL=colas.eventos,
        SQS_PARAMETRICOS_POLIZAS_URL=colas.polizas,
        AWS_ENDPOINT_URL=colas.sqs.meta.endpoint_url,
        SQS_CONCURRENCIA=10,
    )
    lentos = [sobre_observado(observado, casos["valido"], i) for i in range(EN_CURSO)]
    tardios = [sobre_observado(observado, casos["valido"], EN_CURSO + i) for i in range(TARDIOS)]
    poliza_lenta = copy.deepcopy(datos["coberturas"][0]["sobre"])  # COB-BOG-1
    poliza = copy.deepcopy(datos["coberturas"][2]["sobre"])  # COB-MED-1, otra póliza
    eventos_lentos = {UUID(s["eventId"]) for s in lentos}
    compuerta.lentos = eventos_lentos | {UUID(poliza_lenta["eventId"])}
    todos = {UUID(s["eventId"]) for s in lentos + tardios}

    async with AsyncConnectionPool(
        base, min_size=1, max_size=settings.pool_max, open=False
    ) as pool:
        # --- Instancia 1: la app con su lifespan (la tarea que se retira) -------------------------
        app = crear_app(settings, pool=pool, siniestros=SiniestrosQueNoSeLlaman())  # type: ignore[arg-type]
        logging.getLogger().addHandler(caplog.handler)  # crear_app configura el logger raíz
        caplog.set_level(logging.INFO)
        lista, cerrar = asyncio.Event(), asyncio.Event()

        async def instancia() -> None:
            async with app.router.lifespan_context(app):
                lista.set()
                await cerrar.wait()

        tarea = asyncio.create_task(instancia())
        await asyncio.wait_for(lista.wait(), 30)
        assert len(consumidores_creados) == 2
        for sobre in lentos:
            await asyncio.to_thread(colas.enviar, colas.eventos, sobre)
        await asyncio.to_thread(colas.enviar, colas.polizas, poliza_lenta)
        await esperar(lambda: len(compuerta.en_curso) == EN_CURSO + 1, 30, "lo retenido en curso")
        await asyncio.sleep(0.5)  # cada consumidor vuelve a su espera larga

        inicio = time.monotonic()
        cerrar.set()
        await esperar(
            lambda: all(c._parada is not None and c._parada.is_set() for c in consumidores_creados),
            5,
            "la parada de los dos consumidores a la vez, con lo de cada uno en curso",
        )
        ids_tardios = {
            await asyncio.to_thread(colas.enviar, colas.eventos, sobre) for sobre in tardios
        }
        id_poliza = await asyncio.to_thread(colas.enviar, colas.polizas, poliza)

        def liberados() -> set[str]:
            return {
                r.message_id  # type: ignore[attr-defined]
                for r in caplog.records
                if r.getMessage() == MENSAJE_LIBERADO
            }

        await esperar(
            lambda: id_poliza in liberados() and liberados() & ids_tardios,
            10,
            "las esperas largas abandonadas liberan lo que traen",
        )
        assert not tarea.done(), "el cierre no esperó a los que estaban en curso"
        compuerta.abierta.set()
        await asyncio.wait_for(tarea, 30)
        duracion = time.monotonic() - inicio
        print(f"\nHU-W25 drenaje: cierre en {duracion:.1f} s; liberados {len(liberados())}")
        assert duracion < main.PARADA_CONSUMIDOR_S, duracion
        assert liberados() <= ids_tardios | {id_poliza}, "se liberó uno que estaba en curso"

        # Lo que estaba en curso terminó y se borró; lo recibido sin empezar quedó visible ya.
        evaluados = {fila[0] for fila in sql(base, "SELECT evento_id FROM eventos_evaluados")}
        assert evaluados == eventos_lentos
        assert sql(base, "SELECT count(*) FROM coberturas_activas")[0][0] == 1
        await esperar(
            lambda: (
                colas.conteo(colas.eventos) == (TARDIOS, 0)
                and colas.conteo(colas.polizas) == (1, 0)
            ),
            5,
            f"eventos {colas.conteo(colas.eventos)} y pólizas {colas.conteo(colas.polizas)} "
            f"(visibles, en vuelo) deberían ser ({TARDIOS}, 0) y (1, 0)",
        )

        # --- Instancia 2: otra tarea los toma sin esperar la visibilidad --------------------------
        otra = cliente_sqs(REGION, endpoint_url=colas.sqs.meta.endpoint_url, espera_larga_s=1)
        parada = asyncio.Event()
        inicio_otra = time.monotonic()
        tareas = [
            asyncio.create_task(c.ejecutar(parada))
            for c in armar_consumidores(
                pool,
                otra,
                RelojSistema(),
                settings,
                visibilidad_s=VISIBILIDAD_COLA_S,
                espera_larga_s=1,
            ).values()
        ]
        try:
            await esperar(
                lambda: (
                    sql(base, "SELECT count(*) FROM eventos_evaluados")[0][0] == len(todos)
                    and sql(base, "SELECT count(*) FROM coberturas_activas")[0][0] == 2
                ),
                VISIBILIDAD_COLA_S / 2,
                "la otra instancia procesa lo liberado",
            )
            demora_otra = time.monotonic() - inicio_otra
            await esperar(
                lambda: (
                    colas.conteo(colas.eventos) == (0, 0) and colas.conteo(colas.polizas) == (0, 0)
                ),
                5,
                "las colas quedan vacías",
            )
        finally:
            parada.set()
            await asyncio.wait_for(asyncio.gather(*tareas), 20)

    print(f"HU-W25 drenaje: la otra instancia terminó en {demora_otra:.1f} s")
    assert demora_otra < VISIBILIDAD_COLA_S / 2
    # Ninguno perdido ni repetido: una fila por evento, una en el inbox y un evento de salida.
    filas = sql(base, "SELECT evento_id FROM eventos_evaluados")
    assert len(filas) == len(todos) and {f[0] for f in filas} == todos
    assert sql(base, "SELECT count(*) FROM inbox")[0][0] == len(todos) + 2
    salida = sql(
        base, "SELECT aggregate_id FROM outbox WHERE event_type = 'EventoParametricoEvaluado'"
    )
    assert sorted(f[0] for f in salida) == sorted(todos)
    assert sql(base, "SELECT count(*) FROM fallos_consumo")[0][0] == 0
    for url in (colas.eventos, colas.polizas):
        assert colas.conteo(f"{url}-dlq") == (0, 0)
