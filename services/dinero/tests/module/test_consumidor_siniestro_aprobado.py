"""MT-W23-01: dinero consume `SiniestroAprobado`, paga y publica el resultado, con PostgreSQL,
LocalStack 4.14.0 (cola con DLQ, `maxReceiveCount` = 5) y un WireMock en lugar de adaptador-pagos
(respuestas fijas según su contrato; la cadena real es el recorrido local de R7).

El consumidor es el de producción (`main.armar_consumidor_indemnizaciones`). Los pagos se cuentan en
el journal del WireMock: ningún evento repetido, de otro `eventId` o concurrente paga dos veces
(AC2). Un timeout o un 5xx deja la indemnización PENDIENTE y nunca se toma como rechazo (AC3).
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
from dinero.adapters.inbound.sqs.siniestro_aprobado import manejador_siniestro_aprobado
from dinero.adapters.outbound.indemnizaciones_pg import IndemnizacionesPg
from dinero.adapters.outbound.outbox_pg import OutboxIndemnizacionesPg
from dinero.adapters.outbound.pagos_http import TIEMPO_AGOTADO, AdaptadorPagosHttp
from dinero.adapters.outbound.reloj import RelojSistema
from dinero.application.indemnizacion.pagar import PagarIndemnizacion
from dinero.config import Settings
from dinero.main import armar_consumidor_indemnizaciones, crear_app
from jsonschema import Draft202012Validator
from psycopg import sql
from solventa_common import correlation
from solventa_common.inbox import ConsumidorInbox, EventoRecibido, TransaccionInboxPg
from solventa_common.outbox import cliente_sns

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
REGION = "us-east-1"
# Filtros de infra/platform/catalog.tf.
FILTRO_SINIESTROS = {"eventType": [{"prefix": "Indemnizacion"}]}
TIMEOUT_PRUEBA_MS = 300
# Beneficiarios sintéticos del WireMock de `conftest.py` (STUBS_DESEMBOLSOS).
BEN_OK = "ben-ok"
BEN_RECHAZA = "ben-rechaza"
BEN_LENTO = "ben-lento"
BEN_CAIDO = "ben-caido"
RETARDO_LENTO_MS = 1500


# --- Datos y lecturas ------------------------------------------------------------------------------
def aprobado(
    beneficiario: str = BEN_OK,
    *,
    event_id: UUID | None = None,
    siniestro: UUID | None = None,
    codigo: str = "88-2026-0442",
    valor: str = "2500000.00",
    correlacion: str = "corr-modulo-w23",
) -> str:
    siniestro = siniestro or uuid4()
    return json.dumps(
        {
            "eventId": str(event_id or uuid4()),
            "aggregateId": str(siniestro),
            "type": "SiniestroAprobado",
            "version": 1,
            "occurredAt": "2026-11-10T15:00:00Z",
            "correlationId": correlacion,
            "payload": {
                "siniestroId": str(siniestro),
                "codigo": codigo,
                "origen": "PORTAL",
                "polizaId": str(uuid4()),
                "clienteId": str(uuid4()),
                "valorIndemnizar": valor,
                "moneda": "COP",
                "referenciaPago": f"IND-{codigo}-01",
                "beneficiarioRef": beneficiario,
                "aprobadoEn": "2026-11-10T15:00:00Z",
                "decididoPor": str(uuid4()),
            },
        }
    )


def filas(url: str, sql: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.execute(sql).fetchall()


def indemnizaciones(url: str) -> list[tuple[Any, ...]]:
    return filas(
        url,
        "SELECT siniestro_id, estado, transaccion_id, motivo, ultimo_incierto, intentos, "
        "duplicados_descartados, monto, correlation_id FROM indemnizaciones",
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
        self.pagos = AdaptadorPagosHttp(cliente, adaptador.url, TIMEOUT_PRUEBA_MS)

    def __call__(self, cola: Any, max_mensajes: int = 2) -> ConsumidorInbox[Any]:
        return armar_consumidor_indemnizaciones(
            self.pool, cola.receptor(max_mensajes), self.pagos, RelojSistema()
        )

    def caso(self) -> PagarIndemnizacion[Any]:
        repo = IndemnizacionesPg(self.pool)
        return PagarIndemnizacion(
            indemnizaciones=repo,
            transacciones=repo,
            eventos=OutboxIndemnizacionesPg(),
            pagos=self.pagos,
            reloj=RelojSistema(),
        )


@pytest.fixture
async def consumidor_de(pool: Any, adaptador: Any) -> Any:
    async with correlation.cliente_async(timeout=httpx.Timeout(1.0)) as cliente:
        yield Consumidores(pool, adaptador, cliente)


def enviar(cola: Any, cuerpo: str) -> None:
    cola.enviar(cuerpo, "SiniestroAprobado")


# --- AC1: pagar una indemnización ---------------------------------------------------------------------
async def test_confirmado_confirma_con_su_evento_y_el_relay_lo_entrega_a_siniestros_y_auditoria(
    pool: Any, limpia: str, adaptador: Any, localstack: str, nueva_cola: Callable[..., Any]
) -> None:
    """La app real (crear_app) con el consumidor y el relay encendidos, sobre LocalStack."""
    entrada = nueva_cola(nombre="dinero-indemnizaciones")
    para_siniestros = nueva_cola(nombre="siniestros-indemnizaciones")
    auditoria = nueva_cola(nombre="auditoria")
    sns = cliente_sns(REGION, endpoint_url=localstack)
    topico = sns.create_topic(Name=f"eventos-{uuid4().hex[:8]}")["TopicArn"]
    sns.subscribe(
        TopicArn=topico,
        Protocol="sqs",
        Endpoint=para_siniestros.arn,
        Attributes={"RawMessageDelivery": "true", "FilterPolicy": json.dumps(FILTRO_SINIESTROS)},
    )
    sns.subscribe(
        TopicArn=topico,
        Protocol="sqs",
        Endpoint=auditoria.arn,
        Attributes={"RawMessageDelivery": "true"},
    )
    siniestro = uuid4()
    enviar(entrada, aprobado(BEN_OK, siniestro=siniestro, correlacion="corr-modulo-e2e"))
    settings = Settings(
        SQS_DINERO_INDEMNIZACIONES_URL=entrada.url,
        ADAPTADOR_PAGOS_URL=adaptador.url,
        OUTBOX_ENABLED=True,
        EVENTS_TOPIC_ARN=topico,
        AWS_REGION=REGION,
        AWS_ENDPOINT_URL=localstack,
        consumidor_espera_larga_s=1,
        outbox_intervalo_s=0.1,
        indemnizacion_conciliacion_habilitada=False,
        conciliacion_habilitada=False,
    )
    app = crear_app(settings, pool=pool)
    async with app.router.lifespan_context(app):
        recibidos: dict[str, list[dict[str, Any]]] = {"siniestros": [], "auditoria": []}
        limite = time.monotonic() + 30
        while not (recibidos["siniestros"] and recibidos["auditoria"]):
            assert time.monotonic() < limite, "IndemnizacionConfirmada no llegó a las colas"
            for nombre, cola in (("siniestros", para_siniestros), ("auditoria", auditoria)):
                if recibidos[nombre]:
                    continue
                respuesta = await asyncio.to_thread(
                    cola.sqs.receive_message,
                    QueueUrl=cola.url,
                    WaitTimeSeconds=1,
                    MessageAttributeNames=["eventType"],
                )
                recibidos[nombre] = respuesta.get("Messages", [])

    for mensajes in recibidos.values():
        [mensaje] = mensajes
        assert mensaje["MessageAttributes"]["eventType"]["StringValue"] == "IndemnizacionConfirmada"
        sobre = json.loads(mensaje["Body"])
        assert list(validador("IndemnizacionConfirmada").iter_errors(sobre)) == []
        assert sobre["correlationId"] == "corr-modulo-e2e"
        assert sobre["payload"]["siniestroId"] == str(siniestro)
        assert sobre["payload"]["monto"] == "2500000.00"
        assert (sobre["payload"]["intentos"], sobre["payload"]["duplicadosDescartados"]) == (1, 0)
        assert BEN_OK not in mensaje["Body"]
    [(siniestro_db, estado, transaccion, *_)] = indemnizaciones(limpia)
    assert (siniestro_db, estado) == (siniestro, "CONFIRMADA")
    assert transaccion == "TRX-IND-88-2026-0442-01"
    [(tipo, _, _, publicado)] = outbox(limpia)
    assert tipo == "IndemnizacionConfirmada" and publicado is not None
    # Lo que recibió la pasarela: monto en texto, la referencia y la correlación del evento.
    [pago] = adaptador.desembolsos()
    cuerpo = json.loads(pago["body"])
    assert cuerpo == {
        "referencia": "IND-88-2026-0442-01",
        "monto": "2500000.00",
        "moneda": "COP",
        "beneficiarioRef": BEN_OK,
    }
    assert pago["headers"]["X-Correlation-Id"] == "corr-modulo-e2e"
    assert entrada.mensajes(entrada.url) == 0


async def test_aprobado_confirma_con_el_evento_en_el_outbox(
    consumidor_de: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any]
) -> None:
    cola = nueva_cola()
    siniestro = uuid4()
    enviar(cola, aprobado(BEN_OK, siniestro=siniestro, valor="2850000.50"))
    await drenar(consumidor_de(cola), 1)

    [(siniestro_db, estado, transaccion, motivo, incierto, intentos, duplicados, monto, corr)] = (
        indemnizaciones(limpia)
    )
    assert (siniestro_db, estado, motivo, incierto) == (siniestro, "CONFIRMADA", None, None)
    assert (transaccion, intentos, duplicados) == ("TRX-IND-88-2026-0442-01", 1, 0)
    assert monto == Decimal("2850000.5000") and corr == "corr-modulo-w23"
    [(tipo, agregado, payload, publicado)] = outbox(limpia)
    assert (tipo, publicado) == ("IndemnizacionConfirmada", None)  # relay apagado: queda pendiente
    assert (
        list(validador("IndemnizacionConfirmada").iter_errors(_sobre(tipo, agregado, payload)))
        == []
    )
    assert payload["monto"] == "2850000.50" and payload["referencia"] == "IND-88-2026-0442-01"
    assert str(agregado) == payload["indemnizacionId"]
    assert BEN_OK not in json.dumps(payload) and "beneficiario" not in json.dumps(payload)
    assert len(adaptador.desembolsos()) == 1
    assert cola.mensajes(cola.url) == 0


def _sobre(tipo: str, agregado: UUID, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "eventId": str(uuid4()),
        "aggregateId": str(agregado),
        "type": tipo,
        "version": 1,
        "occurredAt": "2026-11-10T15:00:03Z",
        "correlationId": "corr-modulo-w23",
        "payload": payload,
    }


# --- AC4: un rechazo de la pasarela ---------------------------------------------------------------------
async def test_rechazado_rechaza_con_su_motivo_y_su_evento(
    consumidor_de: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any]
) -> None:
    cola = nueva_cola()
    enviar(cola, aprobado(BEN_RECHAZA))
    await drenar(consumidor_de(cola), 1)

    [(_, estado, transaccion, motivo, _, intentos, *_)] = indemnizaciones(limpia)
    assert (estado, transaccion, motivo, intentos) == (
        "RECHAZADA",
        None,
        "BENEFICIARIO_NO_HABILITADO",
        1,
    )
    [(tipo, agregado, payload, _)] = outbox(limpia)
    assert tipo == "IndemnizacionRechazada"
    assert (
        list(validador("IndemnizacionRechazada").iter_errors(_sobre(tipo, agregado, payload))) == []
    )
    assert payload["motivo"] == "BENEFICIARIO_NO_HABILITADO"
    assert len(adaptador.desembolsos()) == 1 and cola.mensajes(cola.url) == 0


# --- AC3: un timeout o un 5xx nunca es un rechazo ----------------------------------------------------
@pytest.mark.parametrize(
    ("beneficiario", "codigo"), [(BEN_LENTO, TIEMPO_AGOTADO), (BEN_CAIDO, "FUENTE_NO_DISPONIBLE")]
)
async def test_un_incierto_deja_pendiente_sin_evento_y_borra_el_mensaje(
    consumidor_de: Any,
    limpia: str,
    adaptador: Any,
    nueva_cola: Callable[..., Any],
    beneficiario: str,
    codigo: str,
) -> None:
    """ben-lento: el WireMock tarda 1,5 s y la prueba corta a 300 ms; ben-caido: 503."""
    cola = nueva_cola(visibilidad_s=30)
    enviar(cola, aprobado(beneficiario))
    inicio = time.monotonic()
    await drenar(consumidor_de(cola), 1)
    if beneficiario == BEN_LENTO:
        assert time.monotonic() - inicio < RETARDO_LENTO_MS / 1000 + 5

    [(_, estado, transaccion, motivo, incierto, intentos, *_)] = indemnizaciones(limpia)
    assert (estado, transaccion, motivo, incierto, intentos) == ("PENDIENTE", None, None, codigo, 1)
    assert outbox(limpia) == []
    assert len(adaptador.desembolsos()) == 1
    # El mensaje se borró: lo pendiente lo resuelve la conciliación, no una reentrega.
    assert cola.mensajes(cola.url) == 0


# --- AC2: ningún pago doble ---------------------------------------------------------------------------
async def test_el_mismo_event_id_dos_veces_paga_una_sola_vez(
    consumidor_de: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any]
) -> None:
    cola = nueva_cola()
    event_id = uuid4()
    cuerpo = aprobado(BEN_OK, event_id=event_id)
    enviar(cola, cuerpo)
    enviar(cola, cuerpo)
    await drenar(consumidor_de(cola), 2)

    assert len(adaptador.desembolsos()) == 1
    assert inbox(limpia) == [event_id]
    assert [fila[1] for fila in indemnizaciones(limpia)] == ["CONFIRMADA"]
    assert [fila[0] for fila in outbox(limpia)] == ["IndemnizacionConfirmada"]
    assert cola.mensajes(cola.url) == 0


async def test_otro_event_id_del_mismo_siniestro_cuenta_un_duplicado_y_no_paga_otra_vez(
    consumidor_de: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any]
) -> None:
    cola = nueva_cola()
    siniestro = uuid4()
    enviar(cola, aprobado(BEN_OK, siniestro=siniestro))
    enviar(cola, aprobado(BEN_OK, siniestro=siniestro))
    enviar(cola, aprobado(BEN_OK, siniestro=siniestro))
    await drenar(consumidor_de(cola), 3)

    [(_, estado, transaccion, _, _, intentos, duplicados, *_)] = indemnizaciones(limpia)
    assert (estado, intentos, duplicados) == ("CONFIRMADA", 1, 2)
    assert transaccion == "TRX-IND-88-2026-0442-01"
    assert len(adaptador.desembolsos()) == 1  # una sola llamada efectiva de pago
    assert len(inbox(limpia)) == 3 and len(outbox(limpia)) == 1
    assert cola.mensajes(cola.url) == 0


async def test_dos_consumidores_a_la_vez_con_el_mismo_siniestro_dejan_una_fila_y_un_pago(
    consumidor_de: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any]
) -> None:
    """Dos consumidores sobre dos colas reciben a la vez un evento distinto del mismo siniestro."""
    siniestro = uuid4()
    colas = [nueva_cola(), nueva_cola()]
    for cola in colas:
        enviar(cola, aprobado(BEN_OK, siniestro=siniestro))
    await asyncio.gather(*(drenar(consumidor_de(cola, 1), 1) for cola in colas))

    [(_, estado, _, _, _, intentos, duplicados, *_)] = indemnizaciones(limpia)
    assert (estado, intentos, duplicados) == ("CONFIRMADA", 1, 1)
    assert len(adaptador.desembolsos()) == 1
    assert [fila[0] for fila in outbox(limpia)] == ["IndemnizacionConfirmada"]


async def test_ocho_eventos_concurrentes_del_mismo_siniestro_pagan_una_sola_vez(
    consumidor_de: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any]
) -> None:
    siniestro = uuid4()
    colas = [nueva_cola() for _ in range(8)]
    for cola in colas:
        enviar(cola, aprobado(BEN_OK, siniestro=siniestro))
    await asyncio.gather(*(drenar(consumidor_de(cola, 1), 1) for cola in colas))

    [(_, estado, _, _, _, intentos, duplicados, *_)] = indemnizaciones(limpia)
    assert (estado, intentos, duplicados) == ("CONFIRMADA", 1, 7)
    assert len(adaptador.desembolsos()) == 1


async def test_una_confirmacion_repetida_de_la_pasarela_suma_un_duplicado_sin_otro_movimiento(
    consumidor_de: Any, pool: Any, limpia: str, adaptador: Any
) -> None:
    """La conciliación se adelantó a la continuación: cuando llega la confirmación de la pasarela,
    la indemnización ya está CONFIRMADA. No hay otro movimiento, otro evento ni otro cambio."""
    caso = consumidor_de.caso()
    reloj = RelojSistema()
    repo = IndemnizacionesPg(pool)
    siniestro = uuid4()
    cuerpo = aprobado(BEN_OK, siniestro=siniestro)
    # El manejador registra la fila (modo b); la continuación todavía no corre.
    async with pool.connection() as conexion, conexion.transaction():
        tx = TransaccionInboxPg(conexion, sql.Identifier("inbox"))
        continuacion = await manejador_siniestro_aprobado(caso)(
            EventoRecibido(
                event_id=uuid4(),
                event_type="SiniestroAprobado",
                correlation_id="corr-modulo-w23",
                sobre={},
                cuerpo=cuerpo,
                message_id="m-1",
                recepciones=1,
            ),
            tx,
        )
    assert continuacion is not None
    [pendiente] = await repo.por_siniestro(siniestro)
    # Otra tarea (la conciliación) ya la confirmó.
    async with repo.transaccion() as tx2:
        assert (
            await repo.registrar_resultado(pendiente.confirmar("TRX-OTRA", reloj.ahora()), tx2) == 1
        )
    await continuacion()

    [(_, estado, transaccion, _, _, intentos, duplicados, *_)] = indemnizaciones(limpia)
    assert (estado, transaccion, intentos, duplicados) == ("CONFIRMADA", "TRX-OTRA", 1, 1)
    assert outbox(limpia) == []  # la conciliación habría escrito el suyo; aquí no hay otro evento
    assert len(adaptador.desembolsos()) == 1  # la pasarela respondió, pero no hay otro movimiento


# --- Entrada inválida ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cuerpo",
    [
        aprobado(valor="2500000.123"),  # más de 2 decimales
        aprobado(valor="0"),  # no mayor que 0
        aprobado(beneficiario=""),  # sin beneficiario
        "no es json",
    ],
)
async def test_un_evento_invalido_no_deja_nada_y_no_se_borra(
    consumidor_de: Any, limpia: str, adaptador: Any, nueva_cola: Callable[..., Any], cuerpo: str
) -> None:
    cola = nueva_cola(visibilidad_s=30)
    enviar(cola, cuerpo)
    await consumidor_de(cola).ejecutar_una_vez()

    assert indemnizaciones(limpia) == [] and outbox(limpia) == [] and inbox(limpia) == []
    assert adaptador.desembolsos() == []
    assert cola.mensajes(cola.url) == 1  # sigue en la cola: a la DLQ tras 5 recepciones
