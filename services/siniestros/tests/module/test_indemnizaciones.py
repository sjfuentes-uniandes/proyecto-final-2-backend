"""MT-W23-03: `siniestros` consume `Indemnizacion*` y refleja el pago (HU-W23; DS3-25), con
PostgreSQL real y LocalStack 4.14.0 (cola con DLQ, `maxReceiveCount` = 5).

El consumidor es el de producción (`main.armar_consumidor_indemnizaciones`): el resultado del pago,
el historial y la fila del inbox se confirman en la misma transacción. Un `Confirmada` repetido o
fuera de orden no cambia nada. Nunca se reemite un evento: el Outbox no se toca.
"""

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb
from siniestros.adapters.outbound.mapeo import copia_a_json, ubicacion_a_json
from siniestros.adapters.outbound.siniestros_pg import SiniestrosPg
from siniestros.application.siniestro.detalle import ConsultarDetalle
from siniestros.application.siniestro.vistas import Plazos
from siniestros.domain.siniestro.modelos import EstadoPaso, EstadoSiniestro, PasoTramite, Siniestro
from siniestros.main import armar_consumidor_indemnizaciones, crear_app

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
AHORA = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
REFERENCIA = "IND-88-2026-0442-01"
ESPERA_LARGA_S = 1
MAX_RECEPCIONES = 5  # maxReceiveCount de siniestros-indemnizaciones en infra/platform/catalog.tf


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


def guardar(conexion: psycopg.Connection[Any], s: Siniestro) -> None:
    conexion.execute(
        "INSERT INTO siniestros (id, codigo, origen, estado, poliza_id, cliente_id, copia_poliza, "
        "causa, fecha_evento, ubicacion_declarada, reportado_en, analista_id, asignado_en, "
        "pausado_desde, pausa_acumulada_s, plazo_soporte_vence_en, decidido_en, version, "
        "correlation_id, valor_reclamado, moneda, deducible, valor_indemnizar) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            s.id,
            s.codigo,
            s.origen.value,
            s.estado.value,
            s.poliza_id,
            s.cliente_id,
            Jsonb(copia_a_json(s.copia_poliza)),
            s.causa,
            s.fecha_evento,
            Jsonb(ubicacion_a_json(s.ubicacion_declarada)),
            s.reportado_en,
            s.analista_id,
            s.asignado_en,
            s.pausado_desde,
            s.pausa_acumulada_s,
            s.plazo_soporte_vence_en,
            s.decidido_en,
            s.version,
            s.correlation_id,
            s.valor_reclamado,
            s.moneda,
            s.deducible,
            s.valor_indemnizar,
        ),
    )


@pytest.fixture
def aprobado(
    siniestro: Callable[..., Siniestro], conexion: psycopg.Connection[Any]
) -> Callable[..., Siniestro]:
    def fabricar(**cambios: Any) -> Siniestro:
        datos: dict[str, Any] = {
            "estado": EstadoSiniestro.APROBADO,
            "analista_id": ANALISTA,
            "asignado_en": AHORA - timedelta(hours=13),
            "decidido_en": AHORA - timedelta(hours=1),
            "deducible": Decimal("350000.00"),
            "valor_indemnizar": Decimal("2500000.00"),
            "codigo": "88-2026-0442",
        }
        datos.update(cambios)
        caso = siniestro(**datos)
        guardar(conexion, caso)
        # El historial que dejaron W19 y W20: de él salen los pasos del trámite.
        for accion, desde, hacia, minutos in (
            ("REPORTADO", None, "REPORTADO", -840),
            ("EVIDENCIAS_VALIDADAS", None, None, -820),
            ("ASIGNADO", "REPORTADO", "EN_EVALUACION", -780),
            ("APROBADO", "EN_EVALUACION", "APROBADO", -60),
        ):
            conexion.execute(
                "INSERT INTO historial_siniestro (id, siniestro_id, accion, estado_anterior, "
                "estado_nuevo, actor_rol, ocurrido_en) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    uuid4(),
                    caso.id,
                    accion,
                    desde,
                    hacia,
                    "ANALISTA_SINIESTROS",
                    AHORA + timedelta(minutes=minutos),
                ),
            )
        return caso

    return fabricar


def sobre_confirmada(siniestro: UUID, *, duplicados: int = 0, transaccion: str = "TRX-1") -> str:
    return json.dumps(
        {
            "eventId": str(uuid4()),
            "aggregateId": str(uuid4()),
            "type": "IndemnizacionConfirmada",
            "version": 1,
            "occurredAt": "2026-11-05T15:00:03Z",
            "correlationId": "corr-modulo-w23",
            "payload": {
                "indemnizacionId": str(uuid4()),
                "siniestroId": str(siniestro),
                "referencia": REFERENCIA,
                "monto": "2500000.00",
                "moneda": "COP",
                "transaccionId": transaccion,
                "intentos": 1,
                "duplicadosDescartados": duplicados,
                "confirmadaEn": "2026-11-05T15:00:03Z",
            },
        }
    )


def sobre_rechazada(siniestro: UUID) -> str:
    return json.dumps(
        {
            "eventId": str(uuid4()),
            "aggregateId": str(uuid4()),
            "type": "IndemnizacionRechazada",
            "version": 1,
            "occurredAt": "2026-11-05T15:00:04Z",
            "correlationId": "corr-modulo-w23",
            "payload": {
                "indemnizacionId": str(uuid4()),
                "siniestroId": str(siniestro),
                "referencia": REFERENCIA,
                "motivo": "BENEFICIARIO_NO_HABILITADO",
                "intentos": 1,
                "duplicadosDescartados": 0,
                "rechazadaEn": "2026-11-05T15:00:04Z",
            },
        }
    )


class ColaSiniestros:
    """Una cola con su DLQ y el receptor de producción."""

    def __init__(self, sqs: Any, url: str, dlq: str) -> None:
        self.sqs, self.url, self.dlq = sqs, url, dlq

    def enviar(self, cuerpo: str, tipo: str) -> None:
        self.sqs.send_message(
            QueueUrl=self.url,
            MessageBody=cuerpo,
            MessageAttributes={"eventType": {"DataType": "String", "StringValue": tipo}},
        )

    def mensajes(self, url: str) -> int:
        atributos = self.sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return sum(int(valor) for valor in atributos.values())

    def receptor(self, max_mensajes: int = 5) -> Any:
        from solventa_common.inbox import ReceptorSqs

        return ReceptorSqs(
            self.sqs, self.url, espera_larga_s=ESPERA_LARGA_S, max_mensajes=max_mensajes
        )


@pytest.fixture
def cola(localstack: str, credenciales: None) -> ColaSiniestros:
    from solventa_common.inbox import cliente_sqs

    sqs = cliente_sqs("us-east-1", endpoint_url=localstack, espera_larga_s=ESPERA_LARGA_S)
    sufijo = uuid4().hex[:8]
    dlq = sqs.create_queue(QueueName=f"siniestros-ind-{sufijo}-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    url = sqs.create_queue(
        QueueName=f"siniestros-ind-{sufijo}",
        Attributes={
            "VisibilityTimeout": "1",
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEPCIONES}
            ),
        },
    )["QueueUrl"]
    return ColaSiniestros(sqs, url, dlq)


async def drenar(consumidor: Any, procesados: int, limite_s: float = 30) -> None:
    total = 0
    limite = time.monotonic() + limite_s
    while total < procesados:
        assert time.monotonic() < limite, f"sin procesar a tiempo ({total} de {procesados})"
        total += await consumidor.ejecutar_una_vez()


def pago(url: str, siniestro: UUID) -> tuple[Any, ...]:
    with psycopg.connect(url, autocommit=True) as c:
        return (
            c.execute(
                "SELECT pago_estado, pago_referencia, pago_transaccion_id, pago_motivo, pago_intentos, "
                "pago_duplicados_descartados FROM siniestros WHERE id = %s",
                (siniestro,),
            ).fetchone()
            or ()
        )


def historial(url: str, siniestro: UUID) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as c:
        return c.execute(
            "SELECT accion, actor_rol, ocurrido_en, detalles, estado_anterior, estado_nuevo "
            "FROM historial_siniestro WHERE siniestro_id = %s AND accion LIKE 'PAGO_%%' "
            "ORDER BY ocurrido_en",
            (siniestro,),
        ).fetchall()


def contar(url: str, tabla: str) -> int:
    with psycopg.connect(url, autocommit=True) as c:
        fila = c.execute(f"SELECT count(*) FROM {tabla}").fetchone()
    assert fila is not None
    return int(fila[0])


# --- AC1: el siniestro refleja el pago ----------------------------------------------------------------
async def test_confirmado_guarda_el_pago_el_historial_y_completa_el_paso_del_tramite(
    pool: Any, limpia: str, aprobado: Callable[..., Siniestro], cola: ColaSiniestros
) -> None:
    caso = aprobado()
    cola.enviar(sobre_confirmada(caso.id, duplicados=1), "IndemnizacionConfirmada")
    await drenar(armar_consumidor_indemnizaciones(pool, cola.receptor(), RelojFijo()), 1)

    assert pago(limpia, caso.id) == ("CONFIRMADO", REFERENCIA, "TRX-1", None, 1, 1)
    [(accion, rol, ocurrido, detalles, anterior, nuevo)] = historial(limpia, caso.id)
    assert (accion, rol, anterior, nuevo) == ("PAGO_CONFIRMADO", "SISTEMA_PAGOS", None, None)
    assert ocurrido == datetime(2026, 11, 5, 15, 0, 3, tzinfo=UTC)
    assert detalles == {
        "referencia": REFERENCIA,
        "intentos": 1,
        "duplicadosDescartados": 1,
        "transaccionId": "TRX-1",
    }
    # Nunca se reemite un evento ni se toca el estado del siniestro.
    assert contar(limpia, "outbox") == 0 and contar(limpia, "inbox") == 1
    with psycopg.connect(limpia, autocommit=True) as c:
        assert c.execute("SELECT estado, version FROM siniestros").fetchall() == [("APROBADO", 1)]
    assert cola.mensajes(cola.url) == 0

    # El detalle lo muestra y el paso «Pago de la indemnización» queda completado.
    detalle = await ConsultarDetalle(
        SiniestrosPg(pool), _Almacen(), _Soportes(), RelojFijo(), Plazos(48, 480, 1440)
    ).ejecutar(caso.id, _actor())
    assert detalle.pago is not None and detalle.pago.estado.value == "CONFIRMADO"
    ultimo = detalle.tramite[-1]
    assert (ultimo.paso, ultimo.estado) == (PasoTramite.PAGO_INDEMNIZACION, EstadoPaso.COMPLETADO)
    assert ultimo.ocurrido_en == datetime(2026, 11, 5, 15, 0, 3, tzinfo=UTC)


def _actor() -> Any:
    from siniestros.application.siniestro.vistas import Actor

    return Actor(str(ANALISTA), "ANALISTA_SINIESTROS", "Luis Mora")


class _Almacen:
    async def verificar(self, clave: str, sha256: str) -> Any:
        from siniestros.domain.siniestro.modelos import IntegridadEvidencia

        return IntegridadEvidencia.VERIFICADA

    async def firmar_lectura(self, clave: str, ttl_s: int, tipo_contenido: str) -> str:
        raise NotImplementedError


class _Soportes:
    def para(self, amparo: str) -> tuple[Any, ...]:
        return ()


# --- AC4: un rechazo queda «por gestionar» ---------------------------------------------------------------
async def test_rechazado_guarda_el_motivo_y_deja_el_paso_del_pago_en_curso(
    pool: Any, limpia: str, aprobado: Callable[..., Siniestro], cola: ColaSiniestros
) -> None:
    caso = aprobado()
    cola.enviar(sobre_rechazada(caso.id), "IndemnizacionRechazada")
    await drenar(armar_consumidor_indemnizaciones(pool, cola.receptor(), RelojFijo()), 1)

    assert pago(limpia, caso.id) == (
        "RECHAZADO",
        REFERENCIA,
        None,
        "BENEFICIARIO_NO_HABILITADO",
        1,
        0,
    )
    [(accion, *_)] = historial(limpia, caso.id)
    assert accion == "PAGO_RECHAZADO"
    detalle = await ConsultarDetalle(
        SiniestrosPg(pool), _Almacen(), _Soportes(), RelojFijo(), Plazos(48, 480, 1440)
    ).ejecutar(caso.id, _actor())
    assert detalle.pago is not None and detalle.pago.motivo == "BENEFICIARIO_NO_HABILITADO"
    assert detalle.tramite[-1].estado is EstadoPaso.ACTUAL  # sigue en curso: pago por gestionar
    with psycopg.connect(limpia, autocommit=True) as c:
        assert c.execute("SELECT estado FROM siniestros").fetchall() == [("APROBADO",)]


# --- Antes del resultado: PENDIENTE derivado --------------------------------------------------------------
async def test_un_caso_aprobado_sin_resultado_se_lee_pendiente_sin_escribir_nada(
    pool: Any, limpia: str, aprobado: Callable[..., Siniestro]
) -> None:
    caso = aprobado()
    detalle = await ConsultarDetalle(
        SiniestrosPg(pool), _Almacen(), _Soportes(), RelojFijo(), Plazos(48, 480, 1440)
    ).ejecutar(caso.id, _actor())
    assert detalle.pago is not None
    assert (detalle.pago.estado.value, detalle.pago.referencia, detalle.pago.intentos) == (
        "PENDIENTE",
        REFERENCIA,
        0,
    )
    assert pago(limpia, caso.id)[0] is None  # derivado al leer: no se escribe
    assert detalle.tramite[-1].estado is EstadoPaso.ACTUAL


# --- Duplicado y desorden ---------------------------------------------------------------------------------
async def test_un_confirmada_tras_otro_confirmada_no_cambia_nada(
    pool: Any, limpia: str, aprobado: Callable[..., Siniestro], cola: ColaSiniestros
) -> None:
    caso = aprobado()
    cola.enviar(sobre_confirmada(caso.id, transaccion="TRX-1"), "IndemnizacionConfirmada")
    cola.enviar(
        sobre_confirmada(caso.id, duplicados=5, transaccion="TRX-OTRA"), "IndemnizacionConfirmada"
    )
    await drenar(armar_consumidor_indemnizaciones(pool, cola.receptor(1), RelojFijo()), 2)

    assert pago(limpia, caso.id) == (
        "CONFIRMADO",
        REFERENCIA,
        "TRX-1",
        None,
        1,
        0,
    )  # gana el primero
    assert len(historial(limpia, caso.id)) == 1
    assert contar(limpia, "inbox") == 2 and cola.mensajes(cola.url) == 0


async def test_un_rechazada_despues_de_un_confirmada_no_cambia_nada(
    pool: Any, limpia: str, aprobado: Callable[..., Siniestro], cola: ColaSiniestros
) -> None:
    caso = aprobado()
    cola.enviar(sobre_confirmada(caso.id), "IndemnizacionConfirmada")
    cola.enviar(sobre_rechazada(caso.id), "IndemnizacionRechazada")
    await drenar(armar_consumidor_indemnizaciones(pool, cola.receptor(1), RelojFijo()), 2)
    assert pago(limpia, caso.id)[0] == "CONFIRMADO" and len(historial(limpia, caso.id)) == 1


async def test_el_mismo_event_id_dos_veces_se_procesa_una_sola_vez(
    pool: Any, limpia: str, aprobado: Callable[..., Siniestro], cola: ColaSiniestros
) -> None:
    caso = aprobado()
    cuerpo = sobre_confirmada(caso.id)
    cola.enviar(cuerpo, "IndemnizacionConfirmada")
    cola.enviar(cuerpo, "IndemnizacionConfirmada")
    await drenar(armar_consumidor_indemnizaciones(pool, cola.receptor(), RelojFijo()), 2)
    assert len(historial(limpia, caso.id)) == 1 and contar(limpia, "inbox") == 1


async def test_dos_consumidores_a_la_vez_con_el_mismo_resultado_escriben_una_sola_vez(
    pool: Any, limpia: str, aprobado: Callable[..., Siniestro], cola: ColaSiniestros
) -> None:
    """Dos eventos distintos del mismo siniestro, procesados a la vez: el `FOR UPDATE` serializa y
    gana el primero."""
    import asyncio

    caso = aprobado()
    for _ in range(4):
        cola.enviar(sobre_confirmada(caso.id), "IndemnizacionConfirmada")
    consumidores = [
        armar_consumidor_indemnizaciones(pool, cola.receptor(2), RelojFijo()) for _ in range(2)
    ]
    await asyncio.gather(*(drenar(c, 2) for c in consumidores))
    assert len(historial(limpia, caso.id)) == 1
    assert pago(limpia, caso.id)[0] == "CONFIRMADO" and contar(limpia, "inbox") == 4


# --- Entrada que no se puede reflejar -----------------------------------------------------------------------
async def test_un_siniestro_que_no_esta_aprobado_se_descarta_y_el_mensaje_se_consume(
    pool: Any, limpia: str, siniestro: Callable[..., Siniestro], conexion: Any, cola: ColaSiniestros
) -> None:
    caso = siniestro(estado=EstadoSiniestro.REPORTADO)
    guardar(conexion, caso)
    cola.enviar(sobre_confirmada(caso.id), "IndemnizacionConfirmada")
    cola.enviar(sobre_confirmada(uuid4()), "IndemnizacionConfirmada")  # un siniestro inexistente
    await drenar(armar_consumidor_indemnizaciones(pool, cola.receptor(), RelojFijo()), 2)
    assert pago(limpia, caso.id)[0] is None and historial(limpia, caso.id) == []
    assert cola.mensajes(cola.url) == 0


async def test_un_evento_invalido_no_deja_nada_y_no_se_borra(
    pool: Any, limpia: str, aprobado: Callable[..., Siniestro], cola: ColaSiniestros
) -> None:
    caso = aprobado()
    cuerpo = json.loads(sobre_confirmada(caso.id))
    cuerpo["payload"]["intentos"] = 0
    cola.enviar(json.dumps(cuerpo), "IndemnizacionConfirmada")
    await armar_consumidor_indemnizaciones(pool, cola.receptor(), RelojFijo()).ejecutar_una_vez()
    assert pago(limpia, caso.id)[0] is None and contar(limpia, "inbox") == 0
    assert cola.mensajes(cola.url) == 1  # a la DLQ tras 5 recepciones


# --- La app real con el consumidor encendido ---------------------------------------------------------------
async def test_la_app_real_consume_de_la_cola_y_refleja_el_pago(
    pool: Any,
    limpia: str,
    aprobado: Callable[..., Siniestro],
    cola: ColaSiniestros,
    localstack: str,
) -> None:
    from siniestros.config import Settings

    caso = aprobado()
    cola.enviar(sobre_confirmada(caso.id), "IndemnizacionConfirmada")
    settings = Settings(
        SQS_SINIESTROS_INDEMNIZACIONES_URL=cola.url,
        AWS_ENDPOINT_URL=localstack,
        consumidor_espera_larga_s=1,
        PLAZOS_HABILITADOS="false",
    )
    app = crear_app(settings, pool=pool)
    async with app.router.lifespan_context(app):
        limite = time.monotonic() + 30
        while pago(limpia, caso.id)[0] is None:
            assert time.monotonic() < limite, "el pago no se reflejó"
            import asyncio

            await asyncio.sleep(0.2)
    assert pago(limpia, caso.id)[0] == "CONFIRMADO"


# --- Contrato: lo que consume es lo que publica dinero --------------------------------------------------------
def test_los_sobres_de_prueba_cumplen_los_esquemas_publicados_por_dinero() -> None:
    siniestro = uuid4()
    for tipo, cuerpo in (
        ("IndemnizacionConfirmada", sobre_confirmada(siniestro)),
        ("IndemnizacionRechazada", sobre_rechazada(siniestro)),
    ):
        esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
        validador = Draft202012Validator(
            esquema, format_checker=Draft202012Validator.FORMAT_CHECKER
        )
        assert list(validador.iter_errors(json.loads(cuerpo))) == []
