"""MT-W21-02 (N2): pagos-parametricos con PostgreSQL y LocalStack SNS y SQS (HU-W21).

Los consumidores son los de `main.armar_consumidores` (la forma de HU-W24 §9) sobre colas reales con
su DLQ (`maxReceiveCount` = 5) suscritas a un tópico con los filtros de `catalog.tf`. Se alimentan
con `contracts/datos-sinteticos/eventos-parametricos.json`: válido, justo en el umbral, una unidad
bajo, fuera de ventana, fuera de zona, duplicado, malformado (mensaje envenenado que termina en la
DLQ) y coberturas desordenadas. Sin llamadas a `siniestros`: las activaciones quedan PENDIENTE.

`TESTCONTAINERS_RYUK_DISABLED=true` en macOS.
"""

import asyncio
import copy
import json
import os
import random
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from jsonschema import Draft202012Validator
from pagos_parametricos.adapters.outbound.descartes_pg import migrar
from pagos_parametricos.adapters.outbound.dlq_sqs import DlqSqs
from pagos_parametricos.adapters.outbound.reloj import RelojSistema
from pagos_parametricos.config import Settings
from pagos_parametricos.main import armar_cola, armar_consumidores
from psycopg_pool import AsyncConnectionPool
from solventa_common.inbox import cliente_sqs

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio

IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
REGION = "us-east-1"
VISIBILIDAD_S = 2
MAX_RECEPCIONES = 5
EVENTOS = Path(__file__).resolve().parents[4] / "contracts" / "events"
MIGRACIONES = (
    Path(__file__).resolve().parents[2] / "src/pagos_parametricos/adapters/outbound/migraciones"
)
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
        migrar(url)  # idempotente
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
def credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def sql(url: str, consulta: str, *parametros: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.execute(consulta, parametros)
        return cursor.fetchall() if cursor.description else []


@pytest.fixture
def limpia(base: str) -> str:
    sql(base, f"TRUNCATE {TABLAS}")
    return base


async def esperar(condicion: Callable[[], Any], plazo_s: float = 40.0) -> Any:
    """Repite `condicion` (en un hilo) hasta que dé un valor verdadero."""
    limite = asyncio.get_running_loop().time() + plazo_s
    while True:
        valor = await asyncio.to_thread(condicion)
        if valor:
            return valor
        if asyncio.get_running_loop().time() > limite:
            raise AssertionError("la condición no se cumplió a tiempo")
        await asyncio.sleep(0.2)


@dataclass
class Entorno:
    """El tópico, las dos colas con su DLQ y los consumidores en marcha."""

    sns: Any
    sqs: Any
    topico: str
    url_eventos: str
    url_polizas: str
    pool: AsyncConnectionPool[Any]
    settings: Settings
    base: str

    @property
    def dlq_eventos(self) -> str:
        return f"{self.url_eventos}-dlq"

    def publicar(self, sobre: dict[str, Any]) -> None:
        self.sns.publish(
            TopicArn=self.topico,
            Message=json.dumps(sobre),
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": sobre["type"]}},
        )

    def enviar(self, cuerpo: str, tipo: str, url: str | None = None) -> None:
        self.sqs.send_message(
            QueueUrl=url or self.url_eventos,
            MessageBody=cuerpo,
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": tipo}},
        )

    def en_dlq(self) -> int:
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=self.dlq_eventos, AttributeNames=["ApproximateNumberOfMessages"]
        )["Attributes"]
        return int(atributos["ApproximateNumberOfMessages"])

    def tabla(self, nombre: str) -> int:
        return int(sql(self.base, f"SELECT count(*) FROM {nombre}")[0][0])


def _crear_cola(sqs: Any, nombre: str) -> str:
    dlq = sqs.create_queue(QueueName=f"{nombre}-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    url: str = sqs.create_queue(
        QueueName=nombre,
        Attributes={
            "VisibilityTimeout": str(VISIBILIDAD_S),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEPCIONES}
            ),
        },
    )["QueueUrl"]
    return url


@pytest.fixture
async def entorno(limpia: str, localstack: str, credenciales: None) -> AsyncIterator[Entorno]:
    import boto3

    sns = boto3.client("sns", region_name=REGION, endpoint_url=localstack)
    sqs = cliente_sqs(REGION, endpoint_url=localstack, espera_larga_s=1)
    sufijo = uuid4().hex[:8]
    topico = sns.create_topic(Name=f"eventos-{sufijo}")["TopicArn"]
    url_eventos = _crear_cola(sqs, f"parametricos-eventos-{sufijo}")
    url_polizas = _crear_cola(sqs, f"parametricos-polizas-{sufijo}")
    for url, filtro in (
        (url_eventos, {"eventType": ["EventoParametricoObservado"]}),
        (url_polizas, {"eventType": [{"prefix": "Poliza"}]}),
    ):
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        sns.subscribe(
            TopicArn=topico,
            Protocol="sqs",
            Endpoint=arn,
            Attributes={"RawMessageDelivery": "true", "FilterPolicy": json.dumps(filtro)},
        )
    settings = Settings(
        SQS_PARAMETRICOS_EVENTOS_URL=url_eventos,
        SQS_PARAMETRICOS_POLIZAS_URL=url_polizas,
        AWS_ENDPOINT_URL=localstack,
        SQS_CONCURRENCIA=10,
    )
    async with AsyncConnectionPool(
        limpia, min_size=1, max_size=settings.pool_max, open=False
    ) as pool:
        consumidores = armar_consumidores(
            pool, sqs, RelojSistema(), settings, visibilidad_s=VISIBILIDAD_S, espera_larga_s=1
        )
        parada = asyncio.Event()
        tareas = [asyncio.create_task(c.ejecutar(parada)) for c in consumidores.values()]
        try:
            yield Entorno(sns, sqs, topico, url_eventos, url_polizas, pool, settings, limpia)
        finally:
            parada.set()
            await asyncio.wait_for(asyncio.gather(*tareas), 20)


def _esquema_evaluado() -> Draft202012Validator:
    esquema = json.loads(
        (EVENTOS / "EventoParametricoEvaluado.v1.schema.json").read_text(encoding="utf-8")
    )
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


async def _cargar_coberturas(entorno: Entorno, datos: dict[str, Any]) -> None:
    for entrada in datos["coberturas"]:
        entorno.publicar(copy.deepcopy(entrada["sobre"]))
    await esperar(lambda: entorno.tabla("coberturas_activas") == len(datos["coberturas"]))


# --- La matriz de evaluación ---------------------------------------------------------------------
async def test_cada_caso_de_la_matriz_se_evalua_una_vez_y_deja_su_rastro(
    entorno: Entorno,
    datos: dict[str, Any],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    await _cargar_coberturas(entorno, datos)
    aceptados = [c for c in datos["eventos"] if c["esperado"]["http"]["estado"] == 202]
    assert len(aceptados) == 8
    for caso in aceptados:
        entorno.publicar(observado(caso))
    await esperar(lambda: entorno.tabla("eventos_evaluados") == 8)

    claves = {c["sobre"]["aggregateId"]: c["clave"] for c in datos["coberturas"]}
    for caso in aceptados:
        evento_id = observado(caso)["eventId"]
        esperado = caso["esperado"]["evaluacion"]
        [fila] = sql(
            entorno.base,
            "SELECT decision, razon, evaluadas, activaciones, fuente, id_externo, version_regla "
            "FROM eventos_evaluados WHERE evento_id = %s",
            UUID(evento_id),
        )
        assert fila[:4] == (
            esperado["decision"],
            esperado["razonEvento"],
            esperado["evaluadas"],
            esperado["activaciones"],
        ), caso["clave"]
        # El origen de la decisión se conserva (SEC-W21-01) y la regla aplicada queda registrada.
        assert (fila[4], fila[5]) == (caso["envio"]["fuente"], caso["envio"]["cuerpo"]["idExterno"])
        evaluaciones = {
            claves[str(poliza)]: {"decision": decision, "razon": razon}
            for poliza, decision, razon in sql(
                entorno.base,
                "SELECT poliza_id, decision, razon FROM evaluaciones WHERE evento_id = %s",
                UUID(evento_id),
            )
        }
        assert evaluaciones == esperado["porPoliza"], caso["clave"]
        versiones = sql(
            entorno.base,
            "SELECT version_regla FROM evaluaciones WHERE evento_id = %s",
            UUID(evento_id),
        )
        assert all(v == ("1",) for v in versiones)

    # Las activaciones: PENDIENTE, sin siniestro (eso es W22), con lo que W22 necesita.
    activas = sum(c["esperado"]["evaluacion"]["activaciones"] for c in aceptados)
    assert entorno.tabla("activaciones") == activas == 4
    for estado, siniestro, valor, moneda, version in sql(
        entorno.base,
        "SELECT estado, siniestro_id, valor_pago, moneda, version_regla FROM activaciones",
    ):
        assert (estado, siniestro, moneda, version) == ("PENDIENTE", None, "COP", "1")
        assert valor in (1400000, 500000)

    # Una fila de Outbox por evento evaluado, que cumple el esquema publicado y trae el origen.
    filas = sql(entorno.base, "SELECT event_type, payload, aggregate_id FROM outbox")
    assert len(filas) == 8 and {f[0] for f in filas} == {"EventoParametricoEvaluado"}
    for _, payload, aggregate_id in filas:
        assert payload["eventoId"] == str(aggregate_id)
        assert payload["fuente"] and payload["idExterno"] and payload["versionRegla"] == "1"
    assert entorno.tabla("inbox") == 8 + 10


async def test_el_evento_evaluado_publicado_cumple_su_esquema(
    entorno: Entorno,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    from solventa_common.outbox import EventoPendiente, sobre_json

    await _cargar_coberturas(entorno, datos)
    entorno.publicar(observado(casos["valido"]))
    await esperar(lambda: entorno.tabla("outbox") == 1)
    [fila] = sql(
        entorno.base,
        "SELECT event_id, aggregate_id, event_type, event_version, occurred_at, correlation_id, "
        "payload::text, attempts FROM outbox",
    )
    sobre = json.loads(sobre_json(EventoPendiente(*fila)))
    assert list(_esquema_evaluado().iter_errors(sobre)) == []
    assert sobre["correlationId"] == "corr-valido"
    assert (sobre["payload"]["decision"], sobre["payload"]["activaciones"]) == ("ACTIVA", 1)


# --- Evaluar una sola vez ------------------------------------------------------------------------
async def test_la_entrega_duplicada_evalua_una_sola_vez(
    entorno: Entorno,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    await _cargar_coberturas(entorno, datos)
    sobre = json.dumps(observado(casos["valido"]))
    for _ in range(5):  # SQS entrega al menos una vez
        entorno.enviar(sobre, "EventoParametricoObservado")
    await esperar(lambda: entorno.tabla("eventos_evaluados") == 1)
    await asyncio.sleep(2.5)  # que las demás entregas lleguen y se descarten
    assert entorno.tabla("eventos_evaluados") == 1
    assert entorno.tabla("evaluaciones") == 2 and entorno.tabla("activaciones") == 1
    assert entorno.tabla("outbox") == 1
    assert entorno.tabla("inbox") == 1 + 10
    assert entorno.en_dlq() == 0


async def test_si_el_inbox_ya_no_recuerda_el_evento_la_clave_de_negocio_conserva_la_decision(
    entorno: Entorno,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """AC4: aunque la fila del inbox se purgue, el evento vuelve a llegar y se conserva la decisión
    original (suma un duplicado), sin otra evaluación, activación ni fila de Outbox."""
    await _cargar_coberturas(entorno, datos)
    sobre = observado(casos["valido"])
    entorno.publicar(sobre)
    await esperar(lambda: entorno.tabla("eventos_evaluados") == 1)
    antes = sql(entorno.base, "SELECT decision, evaluado_en FROM eventos_evaluados")
    sql(entorno.base, "DELETE FROM inbox WHERE event_id = %s", UUID(sobre["eventId"]))
    entorno.enviar(json.dumps(sobre), "EventoParametricoObservado")
    await esperar(lambda: sql(entorno.base, "SELECT duplicados FROM eventos_evaluados")[0][0] == 1)
    assert sql(entorno.base, "SELECT decision, evaluado_en FROM eventos_evaluados") == antes
    assert (entorno.tabla("evaluaciones"), entorno.tabla("activaciones")) == (2, 1)
    assert entorno.tabla("outbox") == 1


async def test_la_clave_unica_evento_poliza_impide_dos_evaluaciones(
    entorno: Entorno, datos: dict[str, Any], casos: dict[str, dict[str, Any]], observado: Any
) -> None:
    await _cargar_coberturas(entorno, datos)
    sobre = observado(casos["valido"])
    entorno.publicar(sobre)
    await esperar(lambda: entorno.tabla("evaluaciones") == 2)
    for tabla in ("evaluaciones", "activaciones"):
        with pytest.raises(psycopg.errors.UniqueViolation):
            sql(
                entorno.base,
                f"INSERT INTO {tabla} (evento_id, poliza_id, "
                + (
                    "decision, evaluado_en) VALUES (%s, %s, 'ACTIVA', now())"
                    if tabla == "evaluaciones"
                    else "valor_pago, moneda, version_regla, creada_en) VALUES (%s, %s, 1, 'COP', '1', now())"
                ),
                UUID(sobre["eventId"]),
                UUID(datos["coberturas"][0]["sobre"]["aggregateId"]),
            )


# --- El mensaje envenenado y la DLQ --------------------------------------------------------------
async def test_los_mensajes_malformados_terminan_en_la_dlq_con_su_causa_y_su_verificacion(
    entorno: Entorno, datos: dict[str, Any]
) -> None:
    from pagos_parametricos.domain.descarte import Causa

    await _cargar_coberturas(entorno, datos)
    sin_zona = next(m for m in datos["mensajesCola"] if m["clave"] == "envenenadoSinZona")
    no_json = next(m for m in datos["mensajesCola"] if m["clave"] == "envenenadoNoJson")
    entorno.enviar(json.dumps(sin_zona["sobre"]), "EventoParametricoObservado")
    entorno.enviar(no_json["cuerpoCrudo"], "EventoParametricoObservado")
    await esperar(lambda: entorno.en_dlq() == 2, plazo_s=60)
    # Nada se evaluó ni se publicó.
    assert (entorno.tabla("eventos_evaluados"), entorno.tabla("outbox")) == (0, 0)

    cola = armar_cola(
        entorno.pool,
        DlqSqs(entorno.sqs, entorno.url_eventos, None),
        RelojSistema(),
        entorno.settings,
    )
    listado = await cola.listar()
    assert len(listado.mensajes) == 2
    assert {m.causa for m in listado.mensajes} == {Causa.FORMATO_INVALIDO}
    assert {m.intentos for m in listado.mensajes} == {MAX_RECEPCIONES}
    # El consumidor anota ambos como envenenados: el sobre ilegible (`json_invalido`) y el que no
    # valida contra su esquema (`EventoInvalido` del manejador, motivo `esquema`).
    fallos = sql(entorno.base, "SELECT outcome, detalle FROM fallos_consumo ORDER BY detalle")
    assert fallos == [("envenenado", "esquema"), ("envenenado", "json_invalido")]

    # H11: el sobre al que solo le falta la zona trae tipo e instante; la verificación se calcula
    # con la copia local (sin zona no hay cobertura de la zona del evento).
    mensaje = next(m for m in listado.mensajes if m.evento_id == sin_zona["sobre"]["eventId"])
    detalle = await cola.detalle(mensaje.message_id)
    assert detalle.verificacion is None  # sin zona ni póliza no hay nada que verificar
    sin_verificar = next(m for m in listado.mensajes if m.message_id != mensaje.message_id)
    assert (await cola.detalle(sin_verificar.message_id)).verificacion is None


async def test_la_verificacion_de_h11_sale_de_la_copia_local(
    entorno: Entorno,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    await _cargar_coberturas(entorno, datos)
    # Un evento de Bogotá con la unidad mal: no valida (a la DLQ), pero trae tipo, zona e instante.
    sobre = observado(casos["valido"])
    sobre["payload"]["unidad"] = "km"
    entorno.enviar(json.dumps(sobre), "EventoParametricoObservado")
    await esperar(lambda: entorno.en_dlq() == 1, plazo_s=60)
    cola = armar_cola(
        entorno.pool,
        DlqSqs(entorno.sqs, entorno.url_eventos, None),
        RelojSistema(),
        entorno.settings,
    )
    [mensaje] = (await cola.listar()).mensajes
    detalle = await cola.detalle(mensaje.message_id)
    assert detalle.verificacion == {
        "polizaVigente": True,
        "coberturaActiva": True,
        "pagoPrevio": False,
        "umbral": "120",
        "valorIndemnizar": "1400000.00",
    }
    assert detalle.mensaje.causa.value == "FORMATO_INVALIDO"

    # Con un siniestro ya creado para ese evento (lo hará W22), el pago previo se ve. Una activación
    # PENDIENTE todavía no es un pago.
    evento = UUID(sobre["eventId"])
    poliza = UUID(datos["coberturas"][0]["sobre"]["aggregateId"])
    sql(
        entorno.base,
        "INSERT INTO eventos_evaluados (evento_id, fuente, id_externo, evento_origen, decision, "
        "evaluadas, activaciones, version_regla, evaluado_en) "
        "VALUES (%s, 'IDEAM-SIM', 'IDEAM-X', 'EVT-X', 'ACTIVA', 1, 1, '1', now())",
        evento,
    )
    sql(
        entorno.base,
        "INSERT INTO activaciones (evento_id, poliza_id, valor_pago, moneda, version_regla, creada_en) "
        "VALUES (%s, %s, 1400000, 'COP', '1', now())",
        evento,
        poliza,
    )
    assert (await cola.detalle(mensaje.message_id)).verificacion["pagoPrevio"] is False  # type: ignore[index]
    sql(entorno.base, "UPDATE activaciones SET siniestro_id = %s", uuid4())
    assert (await cola.detalle(mensaje.message_id)).verificacion["pagoPrevio"] is True  # type: ignore[index]


async def test_corregir_y_reprocesar_un_mensaje_de_la_dlq_evalua_una_sola_vez(
    entorno: Entorno,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """HU-W24 + HU-W21: el analista corrige la unidad y reprocesa con el mismo `eventId`."""
    await _cargar_coberturas(entorno, datos)
    sobre = observado(casos["valido"])
    sobre["payload"]["unidad"] = "km"
    entorno.enviar(json.dumps(sobre), "EventoParametricoObservado")
    await esperar(lambda: entorno.en_dlq() == 1, plazo_s=60)
    cola = armar_cola(
        entorno.pool,
        DlqSqs(entorno.sqs, entorno.url_eventos, None),
        RelojSistema(),
        entorno.settings,
    )
    [mensaje] = (await cola.listar()).mensajes
    await cola.corregir(mensaje.message_id, {"unidad": "mm"}, "actor-1", "corr-h11")
    await esperar(lambda: entorno.tabla("eventos_evaluados") == 1)
    assert entorno.tabla("activaciones") == 1 and entorno.en_dlq() == 0
    [(evento_id,)] = sql(entorno.base, "SELECT evento_id FROM eventos_evaluados")
    assert str(evento_id) == sobre["eventId"]


# --- Coberturas desordenadas ---------------------------------------------------------------------
async def test_los_escenarios_de_orden_en_el_orden_que_dice_el_archivo_de_datos(
    entorno: Entorno, datos: dict[str, Any]
) -> None:
    sobres = {c["clave"]: c["sobre"] for c in [*datos["coberturas"], *datos["cancelaciones"]]}
    procesados = 0
    for escenario in datos["ordenCoberturas"]:
        for clave in escenario["llegada"]:
            entorno.enviar(json.dumps(sobres[clave]), sobres[clave]["type"], entorno.url_polizas)
            procesados += 1
            # De uno en uno: así el orden de llegada es el del archivo.
            await esperar(lambda n=procesados: entorno.tabla("inbox") == n)
    for escenario in datos["ordenCoberturas"]:
        poliza = UUID(sobres[escenario["esperado"]["cobertura"]]["aggregateId"])
        [(estado,)] = sql(
            entorno.base, "SELECT estado FROM coberturas_activas WHERE poliza_id = %s", poliza
        )
        assert estado == escenario["esperado"]["estado"], escenario["clave"]


async def test_con_mensajes_en_cualquier_orden_y_en_paralelo_el_estado_final_es_el_mismo(
    entorno: Entorno, datos: dict[str, Any]
) -> None:
    """Salvo el empate exacto (que depende de quién llega primero), el resultado no depende del
    orden de llegada ni de la concurrencia."""
    sobres = {c["clave"]: c["sobre"] for c in [*datos["coberturas"], *datos["cancelaciones"]]}
    escenarios = [e for e in datos["ordenCoberturas"] if e["clave"] != "ORDEN-5"]
    mensajes = [clave for e in escenarios for clave in e["llegada"]]
    random.Random(21).shuffle(mensajes)
    for clave in mensajes:
        entorno.enviar(json.dumps(sobres[clave]), sobres[clave]["type"], entorno.url_polizas)
    await esperar(lambda: entorno.tabla("inbox") == len(mensajes))
    for escenario in escenarios:
        poliza = UUID(sobres[escenario["esperado"]["cobertura"]]["aggregateId"])
        [(estado,)] = sql(
            entorno.base, "SELECT estado FROM coberturas_activas WHERE poliza_id = %s", poliza
        )
        assert estado == escenario["esperado"]["estado"], escenario["clave"]
    assert entorno.en_dlq() == 0


async def test_las_polizas_sin_cobertura_y_los_demas_eventos_poliza_se_ignoran(
    entorno: Entorno, datos: dict[str, Any]
) -> None:
    sin_cobertura = copy.deepcopy(datos["coberturas"][0]["sobre"])
    del sin_cobertura["payload"]["coberturaParametrica"]
    vencida = {
        "eventId": str(uuid4()),
        "type": "PolizaVencida",
        "version": 1,
        "occurredAt": "2026-11-10T10:00:00Z",
        "payload": {"polizaId": str(uuid4())},
    }
    entorno.publicar(sin_cobertura)
    entorno.publicar(vencida)
    await esperar(lambda: entorno.tabla("inbox") == 2)
    assert entorno.tabla("coberturas_activas") == 0


async def test_una_emision_sin_aggregate_version_no_va_a_la_dlq(
    entorno: Entorno, datos: dict[str, Any]
) -> None:
    """`versionado.leer_version` la habría mandado a la DLQ: `polizas` no publica la versión."""
    await _cargar_coberturas(entorno, datos)
    assert (
        sql(
            entorno.base, "SELECT count(*) FROM coberturas_activas WHERE aggregate_version IS NULL"
        )[0][0]
        >= 8
    )
    assert entorno.tabla("fallos_consumo") == 0


# --- Volumen y concurrencia ----------------------------------------------------------------------
async def test_sesenta_eventos_a_la_vez_se_evaluan_cada_uno_una_vez(
    entorno: Entorno,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    await _cargar_coberturas(entorno, datos)
    sobres = []
    for i in range(60):
        sobre = copy.deepcopy(observado(casos["valido"]))
        evento_id = str(uuid4())
        sobre["eventId"] = sobre["aggregateId"] = sobre["payload"]["eventoId"] = evento_id
        sobre["payload"]["idExterno"] = f"IDEAM-CARGA-{i:04d}"
        sobre["payload"]["valorObservado"] = str(100 + i)
        sobres.append(sobre)
    for sobre in sobres:
        entorno.enviar(json.dumps(sobre), "EventoParametricoObservado")
    # El doble de entregas: lo que SQS puede repetir.
    for sobre in sobres[:20]:
        entorno.enviar(json.dumps(sobre), "EventoParametricoObservado")
    await esperar(lambda: entorno.tabla("eventos_evaluados") == 60)
    await asyncio.sleep(1)
    assert entorno.tabla("eventos_evaluados") == 60
    assert entorno.tabla("evaluaciones") == 120  # dos pólizas de Bogotá por evento
    # Activan los de 120 mm o más: 120 a 159.
    assert entorno.tabla("activaciones") == 40
    assert entorno.tabla("outbox") == 60
    assert entorno.en_dlq() == 0


async def test_dos_consumidores_con_el_mismo_evento_a_la_vez_evaluan_una_vez(
    entorno: Entorno,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Dos réplicas: el INSERT del inbox del segundo espera al commit del primero."""
    await _cargar_coberturas(entorno, datos)
    otro = armar_consumidores(
        entorno.pool,
        entorno.sqs,
        RelojSistema(),
        entorno.settings,
        visibilidad_s=VISIBILIDAD_S,
        espera_larga_s=1,
    )["consumidor_eventos"]
    parada = asyncio.Event()
    tarea = asyncio.create_task(otro.ejecutar(parada))
    try:
        sobre = json.dumps(observado(casos["justoEnElUmbral"]))
        for _ in range(10):
            entorno.enviar(sobre, "EventoParametricoObservado")
        await esperar(lambda: entorno.tabla("eventos_evaluados") == 1)
        await asyncio.sleep(2.5)
    finally:
        parada.set()
        await asyncio.wait_for(tarea, 20)
    assert (entorno.tabla("eventos_evaluados"), entorno.tabla("activaciones")) == (1, 1)
    assert entorno.tabla("outbox") == 1


# --- Sin siniestros, logs y migración ------------------------------------------------------------
async def test_no_se_llama_a_siniestros_y_las_activaciones_quedan_pendientes(
    entorno: Entorno,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    assert entorno.settings.siniestros_url == "http://localhost:8091"  # nadie escucha ahí
    await _cargar_coberturas(entorno, datos)
    entorno.publicar(observado(casos["valido"]))
    await esperar(lambda: entorno.tabla("activaciones") == 1)
    assert sql(entorno.base, "SELECT estado, siniestro_id FROM activaciones") == [
        ("PENDIENTE", None)
    ]
    assert entorno.en_dlq() == 0


def test_la_migracion_002_va_adelante_y_atras(base: str) -> None:
    """Revertir 002 borra sus cinco tablas y deja las de 001 (el outbox es compartido)."""
    reversa = (MIGRACIONES / "down" / "002_parametricos.sql").read_text(encoding="utf-8")
    nuevas = (
        "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
        "('coberturas_activas', 'eventos_evaluados', 'evaluaciones', 'activaciones', 'inbox')"
    )
    antiguas = (
        "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
        "('outbox', 'fallos_consumo', 'resoluciones_descarte')"
    )
    assert sql(base, nuevas) == [(5,)] and sql(base, antiguas) == [(3,)]
    sql(base, reversa)
    sql(base, reversa)  # idempotente
    assert sql(base, nuevas) == [(0,)] and sql(base, antiguas) == [(3,)]
    migrar(base)
    migrar(base)
    assert sql(base, nuevas) == [(5,)]


def test_el_indice_de_candidatas_es_parcial_sobre_las_activas(base: str) -> None:
    [(definicion,)] = sql(
        base,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'coberturas_activas_candidatas'",
    )
    assert "(tipo_evento, zona)" in definicion and "estado = 'ACTIVA'" in definicion


def test_las_restricciones_de_la_copia_de_coberturas(base: str) -> None:
    sql(base, "TRUNCATE evaluaciones, activaciones, eventos_evaluados, coberturas_activas")
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(  # una ACTIVA sin regla
            base,
            "INSERT INTO coberturas_activas (poliza_id, estado, ultimo_evento_en) "
            "VALUES (%s, 'ACTIVA', now())",
            uuid4(),
        )
    sql(  # una CANCELADA sin regla (la cancelación llegó primero) sí
        base,
        "INSERT INTO coberturas_activas (poliza_id, estado, ultimo_evento_en) "
        "VALUES (%s, 'CANCELADA', now())",
        uuid4(),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(  # un evento SIN_POLIZAS exige su razón
            base,
            "INSERT INTO eventos_evaluados (evento_id, fuente, id_externo, evento_origen, decision, "
            "evaluadas, activaciones, version_regla, evaluado_en) "
            "VALUES (%s, 'IDEAM-SIM', 'A', 'B', 'SIN_POLIZAS', 0, 0, '1', now())",
            uuid4(),
        )


async def test_los_logs_no_traen_el_cuerpo_ni_los_valores_medidos(
    entorno: Entorno,
    datos: dict[str, Any],
    casos: dict[str, dict[str, Any]],
    observado: Callable[[dict[str, Any]], dict[str, Any]],
    capturar_logs: Callable[..., Any],
) -> None:
    salida = capturar_logs()
    await _cargar_coberturas(entorno, datos)
    sobre = observado(casos["valido"])
    sobre["payload"]["valorObservado"] = "135.5"
    entorno.publicar(sobre)
    mal = observado(casos["justoEnElUmbral"])
    mal["payload"]["unidad"] = "valor-sensible-km"
    entorno.enviar(json.dumps(mal), "EventoParametricoObservado")
    await esperar(lambda: entorno.tabla("eventos_evaluados") == 1)
    await esperar(lambda: entorno.tabla("fallos_consumo") == 1)
    texto = salida.getvalue()
    assert "parametrico: evento evaluado" in texto and "inbox.consumir" in texto
    for prohibido in ("135.5", "valor-sensible-km", "IDEAM-2026-1110-0001", "EVT-2026-0913"):
        assert prohibido not in texto
