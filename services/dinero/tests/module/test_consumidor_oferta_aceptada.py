"""MT-W16-10 (16.10): dinero consume `OfertaAceptada`, cobra y publica el resultado, con PostgreSQL,
LocalStack 4.14.0 (cola con DLQ, `maxReceiveCount` = 5) y un WireMock en lugar de adaptador-pagos
(respuestas fijas según su contrato; la cadena real es la R3 de la 16.12).

El consumidor es el de producción (`main.armar_consumidor`). Los cobros se cuentan en el journal del
WireMock: ninguna entrega duplicada, concurrente o reentregada cobra dos veces.
"""

import asyncio
import json
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from dinero.adapters.inbound.sqs.oferta_aceptada import manejador_oferta_aceptada
from dinero.adapters.outbound.outbox_pg import OutboxRecaudosPg
from dinero.adapters.outbound.pagos_http import TIEMPO_AGOTADO, AdaptadorPagosHttp
from dinero.adapters.outbound.recaudos_pg import RecaudosPg
from dinero.adapters.outbound.reloj import RelojSistema
from dinero.application.recaudo.recaudar import RecaudarPrima
from dinero.config import Settings
from dinero.main import armar_consumidor, crear_app
from jsonschema import Draft202012Validator
from solventa_common import correlation
from solventa_common.inbox import ConsumidorInbox, InboxPg
from solventa_common.outbox import cliente_sns

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
REGION = "us-east-1"
C01 = "00000000-0000-4000-8000-000000000001"  # aprobado
C02 = "00000000-0000-4000-8000-000000000002"  # rechazado
C06 = "00000000-0000-4000-8000-000000000006"  # lento: dinero corta antes
C07 = "00000000-0000-4000-8000-000000000007"  # 503 FUENTE_NO_DISPONIBLE
# Filtro de polizas-recaudos en infra/platform/catalog.tf.
FILTRO_RECAUDOS = {"eventType": [{"prefix": "Recaudo"}]}


# --- Datos y lecturas ------------------------------------------------------------------------------
def oferta(
    pagador: str = C01,
    *,
    event_id: UUID | None = None,
    clave: UUID | None = None,
    prima: dict[str, str] | None = None,
    correlacion: str = "corr-modulo-w16",
) -> str:
    clave = clave or uuid4()
    return json.dumps(
        {
            "eventId": str(event_id or uuid4()),
            "aggregateId": str(clave),
            "type": "OfertaAceptada",
            "version": 1,
            "occurredAt": "2026-10-28T13:05:26Z",
            "correlationId": correlacion,
            "payload": {
                "aceptacionId": str(clave),
                "cotizacionId": str(uuid4()),
                "ofertaId": None,
                "decisionId": str(uuid4()),
                "clienteId": pagador,
                "productoId": "VIDA-INDIVIDUAL",
                "prima": prima or {"valor": "64638.07", "moneda": "COP", "periodicidad": "MENSUAL"},
                "claveRecaudo": str(clave),
                "huellaCalculo": "sha256:abc",
                "vigenteHasta": "2026-11-27T13:05:26Z",
                "versionDeclaraciones": "2026.10.1",
                "aceptadaEn": "2026-10-28T13:05:26Z",
                "actorId": "asesor-sintetico-01",
                "canal": "PORTAL_ASESOR",
            },
        }
    )


def filas(url: str, sql: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.execute(sql).fetchall()


def recaudos(url: str) -> list[tuple[Any, ...]]:
    return filas(
        url,
        "SELECT clave_recaudo, estado, transaccion_id, motivo, ultimo_incierto, monto, "
        "correlation_id FROM recaudos",
    )


def outbox(url: str) -> list[tuple[Any, ...]]:
    return filas(url, "SELECT event_type, aggregate_id, payload, published_at FROM outbox")


def inbox(url: str) -> list[UUID]:
    return [fila[0] for fila in filas(url, "SELECT event_id FROM inbox")]


def validador(tipo: str) -> Draft202012Validator:
    esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


async def drenar(consumidor: ConsumidorInbox[Any], procesados: int, limite_s: float = 30) -> None:
    """Ciclos hasta procesar `procesados` mensajes (nuevos y duplicados)."""
    total = 0
    limite = time.monotonic() + limite_s
    while total < procesados:
        assert time.monotonic() < limite, f"sin procesar a tiempo ({total} de {procesados})"
        total += await consumidor.ejecutar_una_vez()


class Consumidores:
    """Arma el consumidor de producción sobre la cola y el WireMock dados."""

    def __init__(self, pool: Any, adaptador: Any, cliente: httpx.AsyncClient) -> None:
        self.pool = pool
        self.pagos = AdaptadorPagosHttp(
            cliente, adaptador.url, Settings().adaptador_pagos_timeout_ms
        )

    def __call__(self, cola: Any, max_mensajes: int = 2) -> ConsumidorInbox[Any]:
        return armar_consumidor(self.pool, cola.receptor(max_mensajes), self.pagos, RelojSistema())


@pytest.fixture
async def consumidor_de(pool: Any, adaptador: Any) -> Any:
    async with correlation.cliente_async(timeout=httpx.Timeout(1.0)) as cliente:
        yield Consumidores(pool, adaptador, cliente)


# --- Casos -----------------------------------------------------------------------------------------
async def test_aprobado_confirma_con_su_evento_y_el_relay_lo_entrega_a_polizas_recaudos(
    pool: Any, limpia: str, adaptador: Any, localstack: str, nueva_cola: Callable[..., Any]
) -> None:
    """La app real (crear_app) con el consumidor y el relay encendidos, sobre LocalStack."""
    aceptaciones = nueva_cola()
    recaudos_cola = nueva_cola(nombre="polizas-recaudos")
    sns = cliente_sns(REGION, endpoint_url=localstack)
    topico = sns.create_topic(Name=f"eventos-{uuid4().hex[:8]}")["TopicArn"]
    sns.subscribe(
        TopicArn=topico,
        Protocol="sqs",
        Endpoint=recaudos_cola.arn,
        Attributes={"RawMessageDelivery": "true", "FilterPolicy": json.dumps(FILTRO_RECAUDOS)},
    )
    clave = uuid4()
    aceptaciones.enviar(oferta(C01, clave=clave, correlacion="corr-modulo-e2e"))
    settings = Settings(
        SQS_DINERO_ACEPTACIONES_URL=aceptaciones.url,
        ADAPTADOR_PAGOS_URL=adaptador.url,
        OUTBOX_ENABLED=True,
        EVENTS_TOPIC_ARN=topico,
        AWS_REGION=REGION,
        AWS_ENDPOINT_URL=localstack,
        consumidor_espera_larga_s=1,
        outbox_intervalo_s=0.1,
    )
    app = crear_app(settings, pool=pool)
    async with app.router.lifespan_context(app):
        limite = time.monotonic() + 30
        recibidos: list[dict[str, Any]] = []
        while not recibidos:
            assert time.monotonic() < limite, "RecaudoConfirmado no llegó a polizas-recaudos"
            respuesta = await asyncio.to_thread(
                recaudos_cola.sqs.receive_message,
                QueueUrl=recaudos_cola.url,
                WaitTimeSeconds=1,
                MessageAttributeNames=["eventType"],
            )
            recibidos = respuesta.get("Messages", [])

    [mensaje] = recibidos
    assert mensaje["MessageAttributes"]["eventType"]["StringValue"] == "RecaudoConfirmado"
    sobre = json.loads(mensaje["Body"])
    assert list(validador("RecaudoConfirmado").iter_errors(sobre)) == []
    assert sobre["correlationId"] == "corr-modulo-e2e"
    assert sobre["payload"]["claveRecaudo"] == str(clave)
    assert sobre["payload"]["monto"] == {"valor": "64638.07", "moneda": "COP"}
    assert C01 not in mensaje["Body"]
    [(clave_db, estado, transaccion, *_)] = recaudos(limpia)
    assert (clave_db, estado, transaccion) == (clave, "CONFIRMADO", "TX-SINT-C01")
    [(tipo, _, _, publicado)] = outbox(limpia)
    assert tipo == "RecaudoConfirmado" and publicado is not None
    # Lo que recibió el adaptador: monto en texto, la correlación del evento.
    [cobro] = adaptador.cobros()
    assert json.loads(cobro["body"])["monto"] == "64638.07"
    assert cobro["headers"]["X-Correlation-Id"] == "corr-modulo-e2e"
    assert aceptaciones.mensajes(aceptaciones.url) == 0


async def test_rechazado_rechaza_con_su_evento(
    consumidor_de: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any]
) -> None:
    cola = nueva_cola()
    cola.enviar(oferta(C02))
    await drenar(consumidor_de(cola), 1)

    [(clave, estado, transaccion, motivo, *_)] = recaudos(limpia)
    assert (estado, transaccion, motivo) == ("RECHAZADO", None, "FONDOS_INSUFICIENTES")
    [(tipo, agregado, payload, publicado)] = outbox(limpia)
    assert (tipo, publicado) == ("RecaudoRechazado", None)  # relay apagado: queda pendiente
    assert payload["motivo"] == "FONDOS_INSUFICIENTES" and payload["claveRecaudo"] == str(clave)
    assert str(agregado) == payload["recaudoId"]
    assert len(adaptador.cobros()) == 1
    assert cola.mensajes(cola.url) == 0


@pytest.mark.parametrize(
    ("pagador", "codigo"), [(C06, TIEMPO_AGOTADO), (C07, "FUENTE_NO_DISPONIBLE")]
)
async def test_un_incierto_deja_pendiente_sin_evento_y_borra_el_mensaje(
    consumidor_de: Any,
    limpia: str,
    adaptador: Any,
    nueva_cola: Callable[..., Any],
    pagador: str,
    codigo: str,
) -> None:
    """C06: el WireMock tarda 8 s y dinero corta a los 6,5 s; C07: 503. Nunca se asume rechazo."""
    cola = nueva_cola(visibilidad_s=30)
    cola.enviar(oferta(pagador))
    inicio = time.monotonic()
    await drenar(consumidor_de(cola), 1)
    if pagador == C06:
        assert 6 <= time.monotonic() - inicio < 8

    [(_, estado, transaccion, motivo, incierto, *_)] = recaudos(limpia)
    assert (estado, transaccion, motivo, incierto) == ("PENDIENTE", None, None, codigo)
    assert outbox(limpia) == []
    assert len(adaptador.cobros()) == 1
    # El mensaje se borró: lo pendiente lo resuelve la conciliación (16.11), no una reentrega.
    assert cola.mensajes(cola.url) == 0


async def test_el_mismo_event_id_dos_veces_cobra_una_sola_vez(
    consumidor_de: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any]
) -> None:
    cola = nueva_cola()
    event_id = uuid4()
    cuerpo = oferta(C01, event_id=event_id)
    cola.enviar(cuerpo)
    cola.enviar(cuerpo)
    await drenar(consumidor_de(cola), 2)

    assert len(adaptador.cobros()) == 1
    assert inbox(limpia) == [event_id]
    assert [fila[1] for fila in recaudos(limpia)] == ["CONFIRMADO"]
    assert [fila[0] for fila in outbox(limpia)] == ["RecaudoConfirmado"]
    assert cola.mensajes(cola.url) == 0


async def test_dos_event_id_distintos_con_la_misma_clave_cobran_una_sola_vez(
    consumidor_de: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any]
) -> None:
    """Otra publicación de la misma aceptación (p. ej., un reintento del relay de polizas con otro
    eventId): el índice único de `claveRecaudo` no deja cobrar de nuevo."""
    cola = nueva_cola()
    clave = uuid4()
    primero, segundo = uuid4(), uuid4()
    cola.enviar(oferta(C01, event_id=primero, clave=clave))
    cola.enviar(oferta(C01, event_id=segundo, clave=clave))
    await drenar(consumidor_de(cola), 2)

    assert len(adaptador.cobros()) == 1
    assert sorted(inbox(limpia)) == sorted([primero, segundo])
    assert [(fila[0], fila[1]) for fila in recaudos(limpia)] == [(clave, "CONFIRMADO")]
    assert len(outbox(limpia)) == 1
    assert cola.mensajes(cola.url) == 0


async def test_dos_consumidores_concurrentes_con_el_duplicado_cobran_una_sola_vez(
    consumidor_de: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any]
) -> None:
    cola = nueva_cola(visibilidad_s=10)
    cuerpo = oferta(C01)
    cola.enviar(cuerpo)
    cola.enviar(cuerpo)
    consumidores = [consumidor_de(cola, max_mensajes=1) for _ in range(2)]
    resultados = await asyncio.gather(*(c.ejecutar_una_vez() for c in consumidores))

    assert sum(resultados) == 2  # uno nuevo y uno duplicado (o los dos en el mismo ciclo)
    assert len(adaptador.cobros()) == 1
    assert len(inbox(limpia)) == 1 and len(recaudos(limpia)) == 1
    assert len(outbox(limpia)) == 1
    assert cola.mensajes(cola.url) == 0


@pytest.mark.parametrize(
    "prima",
    [
        {"valor": "64638.07", "moneda": "USD", "periodicidad": "MENSUAL"},
        {"valor": "64638.071", "moneda": "COP", "periodicidad": "MENSUAL"},
    ],
    ids=["moneda", "decimales"],
)
async def test_un_envenenado_va_a_la_dlq_tras_5_recepciones_sin_cobrar(
    consumidor_de: Any,
    limpia: str,
    adaptador: Any,
    nueva_cola: Callable[..., Any],
    prima: dict[str, str],
) -> None:
    cola = nueva_cola()
    cuerpo = oferta(C01, prima=prima)
    cola.enviar(cuerpo)
    consumidor = consumidor_de(cola)
    limite = time.monotonic() + 40
    while cola.mensajes(cola.dlq) == 0:
        assert time.monotonic() < limite, "el mensaje no llegó a la DLQ"
        assert await consumidor.ejecutar_una_vez() == 0

    assert adaptador.cobros() == []
    assert inbox(limpia) == [] and recaudos(limpia) == [] and outbox(limpia) == []
    assert cola.mensajes(cola.url) == 0
    respuesta = cola.sqs.receive_message(
        QueueUrl=cola.dlq,
        WaitTimeSeconds=1,
        MessageSystemAttributeNames=["ApproximateReceiveCount"],
    )
    [en_dlq] = respuesta["Messages"]
    assert en_dlq["Body"] == cuerpo


class FallaUnaVez:
    """`Transacciones` real que no abre la primera: la base cae entre el cobro y el resultado."""

    def __init__(self, real: RecaudosPg) -> None:
        self.real = real
        self.fallos = 1

    def transaccion(self) -> Any:
        if self.fallos:
            self.fallos -= 1
            raise psycopg.OperationalError("la base no responde")
        return self.real.transaccion()


async def test_caida_de_la_base_tras_el_cobro_no_cobra_de_nuevo_en_la_reentrega(
    pool: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any]
) -> None:
    cola = nueva_cola()
    cola.enviar(oferta(C01))
    recaudos_pg = RecaudosPg(pool)
    async with correlation.cliente_async(timeout=httpx.Timeout(1.0)) as cliente:
        caso = RecaudarPrima(
            recaudos=recaudos_pg,
            transacciones=FallaUnaVez(recaudos_pg),
            eventos=OutboxRecaudosPg(),
            pagos=AdaptadorPagosHttp(cliente, adaptador.url, 6500),
            reloj=RelojSistema(),
        )
        consumidor = ConsumidorInbox(
            cola.receptor(), InboxPg(pool), manejador_oferta_aceptada(caso)
        )
        # Primera entrega: commit del PENDIENTE, cobro y caída: el mensaje no se borra.
        assert await consumidor.ejecutar_una_vez() == 0
        assert len(adaptador.cobros()) == 1
        assert cola.mensajes(cola.url) == 1
        # Reentrega tras la visibilidad: duplicado en el inbox, se borra sin cobrar.
        await drenar(consumidor, 1)

    assert len(adaptador.cobros()) == 1
    [(_, estado, transaccion, _, incierto, monto, _)] = recaudos(limpia)
    assert (estado, transaccion, incierto) == ("PENDIENTE", None, None)
    assert monto == Decimal("64638.07")
    assert outbox(limpia) == []
    assert cola.mensajes(cola.url) == 0 and cola.mensajes(cola.dlq) == 0
