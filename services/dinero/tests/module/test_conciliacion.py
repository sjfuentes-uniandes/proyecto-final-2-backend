"""MT-W16-11 (16.11): conciliación de los PENDIENTE con PostgreSQL y un WireMock en lugar de
adaptador-pagos (respuestas fijas según su contrato; la cadena real es la R3 de la 16.12).

La conciliación es la de producción (`main.armar_conciliacion`). Los cobros y las consultas se
cuentan en el journal del WireMock; los eventos, en el outbox. Para no esperar la antigüedad
mínima real (5 min), unas pruebas crean los movimientos con fecha pasada y otras concilian con un
reloj adelantado.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from dinero.adapters.outbound.outbox_pg import OutboxRecaudosPg
from dinero.adapters.outbound.pagos_http import TIEMPO_AGOTADO, AdaptadorPagosHttp
from dinero.adapters.outbound.recaudos_pg import RecaudosPg
from dinero.adapters.outbound.reloj import RelojSistema
from dinero.application.recaudo import conciliar
from dinero.application.recaudo.conciliar import ConciliarRecaudos, Resultado
from dinero.application.recaudo.recaudar import RecaudarPrima
from dinero.config import Settings
from dinero.domain.recaudo.modelos import Recaudo
from dinero.main import armar_conciliacion, crear_app
from jsonschema import Draft202012Validator
from solventa_common import correlation

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
# Pagadores del WireMock (conftest): cobro y consulta según el contrato del adaptador.
C06 = "00000000-0000-4000-8000-000000000006"  # cobro lento (8 s); consulta APROBADO
C07 = "00000000-0000-4000-8000-000000000007"  # cobro 503; consulta 404 COBRO_NO_ENCONTRADO
R01 = "00000000-0000-4000-9000-000000000001"  # consulta 404; reintento APROBADO
L01 = "00000000-0000-4000-9000-000000000002"  # cobro y consulta APROBADO en 500 ms
I01 = "00000000-0000-4000-9000-000000000003"  # consulta 503 FUENTE_NO_DISPONIBLE
CORRELACION = "corr-modulo-conciliacion"
# Más que la antigüedad mínima por defecto (300 s).
HACE_10_MIN = timedelta(minutes=10)


# --- Datos y lecturas ------------------------------------------------------------------------------
class RelojAdelantado:
    """El reloj del sistema, 10 minutos en el futuro: lo recién creado ya es antiguo."""

    def ahora(self) -> datetime:
        return datetime.now(UTC) + HACE_10_MIN


async def pendiente(
    recaudos: RecaudosPg, pagador: str, *, hace: timedelta = HACE_10_MIN
) -> Recaudo:
    """Un PENDIENTE creado hace `hace` (sin pasar por la cola ni cobrar)."""
    recaudo = Recaudo.pendiente(
        clave_recaudo=uuid4(),
        cotizacion_id=uuid4(),
        pagador_ref=UUID(pagador),
        monto=Decimal("64638.07"),
        moneda="COP",
        periodicidad="MENSUAL",
        correlation_id=CORRELACION,
        ahora=datetime.now(UTC) - hace,
    )
    assert (await recaudos.crear_pendiente(recaudo)).nuevo
    return recaudo


def filas(url: str, sql: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.execute(sql).fetchall()


def movimientos(url: str) -> dict[UUID, tuple[Any, ...]]:
    return {
        fila[0]: fila[1:]
        for fila in filas(
            url,
            "SELECT clave_recaudo, estado, transaccion_id, motivo, ultimo_incierto, "
            "actualizado_en FROM recaudos",
        )
    }


def outbox(url: str) -> list[tuple[Any, ...]]:
    return filas(url, "SELECT event_type, aggregate_id, payload, correlation_id FROM outbox")


def validador(tipo: str) -> Draft202012Validator:
    esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def clave_de(solicitud: dict[str, Any]) -> str:
    return solicitud["url"].split("?", 1)[0].rsplit("/", 1)[-1]


@pytest.fixture
async def pagos(adaptador: Any) -> Any:
    async with correlation.cliente_async(timeout=httpx.Timeout(1.0)) as cliente:
        yield AdaptadorPagosHttp(cliente, adaptador.url, Settings().adaptador_pagos_timeout_ms)


@pytest.fixture
def conciliacion(pool: Any, pagos: AdaptadorPagosHttp) -> Callable[..., ConciliarRecaudos[Any]]:
    def armar(reloj: Any = None, **cambios: Any) -> ConciliarRecaudos[Any]:
        return armar_conciliacion(pool, pagos, reloj or RelojSistema(), Settings(**cambios))

    return armar


# --- Casos -----------------------------------------------------------------------------------------
async def test_un_incierto_por_timeout_se_confirma_al_consultar_c06(
    recaudos: RecaudosPg,
    limpia: str,
    adaptador: Any,
    pagos: AdaptadorPagosHttp,
    conciliacion: Callable[..., ConciliarRecaudos[Any]],
) -> None:
    """C06: la continuación de la 16.10 cobra, el cobro tarda 8 s y dinero corta a los 6,5 s
    (PENDIENTE, sin evento); la consulta responde APROBADO: el cobro sí ocurrió. Sin reintento."""
    movimiento = await pendiente(recaudos, C06, hace=timedelta(0))
    clave = movimiento.clave_recaudo
    continuacion = RecaudarPrima(
        recaudos=recaudos,
        transacciones=recaudos,
        eventos=OutboxRecaudosPg(),
        pagos=pagos,
        reloj=RelojSistema(),
    )
    await continuacion.cobrar(movimiento)
    assert movimientos(limpia)[clave][:4] == ("PENDIENTE", None, None, TIEMPO_AGOTADO)
    assert outbox(limpia) == []

    [conciliado] = await conciliacion(RelojAdelantado()).conciliar_una_vez()

    assert (conciliado.clave_recaudo, conciliado.resultado) == (clave, Resultado.CONFIRMADO)
    estado, transaccion, motivo, incierto, _ = movimientos(limpia)[clave]
    # El código del incierto se conserva al resolver.
    assert (estado, transaccion, motivo, incierto) == (
        "CONFIRMADO",
        "TX-SINT-C06",
        None,
        TIEMPO_AGOTADO,
    )
    [(tipo, agregado, payload, correlacion)] = outbox(limpia)
    assert tipo == "RecaudoConfirmado" and correlacion == CORRELACION
    sobre = {
        "eventId": str(uuid4()),
        "aggregateId": str(agregado),
        "type": tipo,
        "version": 1,
        "occurredAt": payload["confirmadoEn"],
        "correlationId": correlacion,
        "payload": payload,
    }
    assert list(validador("RecaudoConfirmado").iter_errors(sobre)) == []
    assert payload["claveRecaudo"] == str(clave) and payload["transaccionId"] == "TX-SINT-C06"
    assert C06 not in json.dumps(payload)
    # Un solo cobro (el del consumidor) y una consulta, con el pagador y la correlación.
    assert len(adaptador.cobros()) == 1
    [consulta] = adaptador.consultas()
    assert clave_de(consulta) == str(clave)
    assert consulta["queryParams"]["pagadorRef"]["values"] == [C06]
    assert consulta["headers"]["X-Correlation-Id"] == CORRELACION


async def test_no_encontrado_reintenta_una_vez_con_la_misma_clave_y_confirma(
    recaudos: RecaudosPg,
    limpia: str,
    adaptador: Any,
    conciliacion: Callable[..., ConciliarRecaudos[Any]],
) -> None:
    movimiento = await pendiente(recaudos, R01)

    [conciliado] = await conciliacion().conciliar_una_vez()

    assert (conciliado.resultado, conciliado.reintento) == (Resultado.CONFIRMADO, True)
    assert movimientos(limpia)[movimiento.clave_recaudo][:2] == ("CONFIRMADO", "TX-SINT-R01")
    assert [fila[0] for fila in outbox(limpia)] == ["RecaudoConfirmado"]
    [consulta] = adaptador.consultas()
    [reintento] = adaptador.cobros()
    assert clave_de(consulta) == str(movimiento.clave_recaudo)
    cuerpo = json.loads(reintento["body"])
    assert cuerpo == {
        "claveRecaudo": str(movimiento.clave_recaudo),
        "monto": "64638.07",
        "moneda": "COP",
        "pagadorRef": R01,
    }
    assert reintento["headers"]["X-Correlation-Id"] == CORRELACION


async def test_no_encontrado_con_reintento_incierto_sigue_pendiente_sin_evento(
    recaudos: RecaudosPg,
    limpia: str,
    adaptador: Any,
    conciliacion: Callable[..., ConciliarRecaudos[Any]],
) -> None:
    """C07: la consulta responde 404 COBRO_NO_ENCONTRADO y el reintento, 503. Nunca RECHAZADO."""
    movimiento = await pendiente(recaudos, C07)
    antes = movimientos(limpia)[movimiento.clave_recaudo][4]

    [conciliado] = await conciliacion().conciliar_una_vez()

    assert (conciliado.resultado, conciliado.reintento) == (Resultado.INCIERTO, True)
    estado, transaccion, motivo, incierto, actualizado = movimientos(limpia)[
        movimiento.clave_recaudo
    ]
    assert (estado, transaccion, motivo, incierto) == (
        "PENDIENTE",
        None,
        None,
        "FUENTE_NO_DISPONIBLE",
    )
    # Renueva la antigüedad: el próximo intento espera otra antigüedad mínima.
    assert actualizado > antes
    assert outbox(limpia) == []
    assert len(adaptador.consultas()) == 1 and len(adaptador.cobros()) == 1
    # El ciclo siguiente ya no lo toma: es reciente.
    assert await conciliacion().conciliar_una_vez() == []
    assert len(adaptador.consultas()) == 1


async def test_la_continuacion_y_la_conciliacion_a_la_vez_dejan_un_solo_evento(
    pool: Any,
    recaudos: RecaudosPg,
    limpia: str,
    adaptador: Any,
    pagos: AdaptadorPagosHttp,
    conciliacion: Callable[..., ConciliarRecaudos[Any]],
) -> None:
    """L01 responde a las dos llamadas en 500 ms: el cobro de la continuación y la consulta de la
    conciliación están en vuelo a la vez, y la guarda de PENDIENTE deja pasar solo a una."""
    movimiento = await pendiente(recaudos, L01)
    caso = RecaudarPrima(
        recaudos=recaudos,
        transacciones=recaudos,
        eventos=OutboxRecaudosPg(),
        pagos=pagos,
        reloj=RelojSistema(),
    )

    _, conciliados = await asyncio.gather(
        caso.cobrar(movimiento), conciliacion().conciliar_una_vez()
    )

    # Las dos llamadas llegaron al adaptador: la concurrencia fue real.
    assert len(adaptador.cobros()) == 1 and len(adaptador.consultas()) == 1
    assert movimientos(limpia)[movimiento.clave_recaudo][:2] == ("CONFIRMADO", "TX-SINT-L01")
    assert [fila[0] for fila in outbox(limpia)] == ["RecaudoConfirmado"]
    [conciliado] = conciliados
    assert conciliado.resultado in (Resultado.CONFIRMADO, Resultado.YA_RESUELTO)


async def test_dos_conciliaciones_concurrentes_dejan_un_solo_evento(
    recaudos: RecaudosPg,
    limpia: str,
    adaptador: Any,
    conciliacion: Callable[..., ConciliarRecaudos[Any]],
) -> None:
    """Dos tareas de dinero toman el mismo PENDIENTE (sin bloqueos) y consultan a la vez."""
    movimiento = await pendiente(recaudos, L01)

    primera, segunda = await asyncio.gather(
        conciliacion().conciliar_una_vez(), conciliacion().conciliar_una_vez()
    )

    assert len(adaptador.consultas()) == 2 and adaptador.cobros() == []
    assert movimientos(limpia)[movimiento.clave_recaudo][0] == "CONFIRMADO"
    assert [fila[0] for fila in outbox(limpia)] == ["RecaudoConfirmado"]
    assert sorted(c.resultado for c in primera + segunda) == sorted(
        [Resultado.CONFIRMADO, Resultado.YA_RESUELTO]
    )


async def test_un_pendiente_de_mas_de_una_hora_con_consulta_incierta_se_mide_como_error(
    recaudos: RecaudosPg,
    limpia: str,
    adaptador: Any,
    conciliacion: Callable[..., ConciliarRecaudos[Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Con la alerta por defecto (1 h): creado hace 2 h, la consulta responde 503. El resultado
    medido es `error` y el log, ERROR; el movimiento sigue PENDIENTE, sin evento, y el siguiente
    ciclo lo vuelve a consultar (sin tope)."""
    movimiento = await pendiente(recaudos, I01, hace=timedelta(hours=2))
    caplog.set_level("INFO")

    [conciliado] = await conciliacion().conciliar_una_vez()

    assert (conciliado.resultado, conciliado.vencido) == (Resultado.ERROR, True)
    assert conciliado.codigo == "FUENTE_NO_DISPONIBLE"
    estado, transaccion, motivo, incierto, _ = movimientos(limpia)[movimiento.clave_recaudo]
    assert (estado, transaccion, motivo, incierto) == (
        "PENDIENTE",
        None,
        None,
        "FUENTE_NO_DISPONIBLE",
    )
    assert outbox(limpia) == []
    assert adaptador.cobros() == []
    medicion = [r for r in caplog.records if getattr(r, "operation", None) == "recaudo.conciliar"]
    assert [r.result for r in medicion] == ["error"]  # type: ignore[attr-defined]
    [vencido] = [r for r in caplog.records if r.getMessage() == conciliar.MENSAJE_VENCIDO]
    assert vencido.levelname == "ERROR"
    assert vencido.clave_recaudo == str(movimiento.clave_recaudo)  # type: ignore[attr-defined]
    assert vencido.antiguedad_s >= 7200  # type: ignore[attr-defined]
    assert I01 not in caplog.text
    # Sin tope: pasada la antigüedad mínima, se vuelve a conciliar.
    [otra_vez] = await conciliacion(RelojAdelantado()).conciliar_una_vez()
    assert (otra_vez.clave_recaudo, otra_vez.vencido) == (movimiento.clave_recaudo, True)


async def test_un_movimiento_reciente_no_se_consulta(
    recaudos: RecaudosPg,
    limpia: str,
    adaptador: Any,
    conciliacion: Callable[..., ConciliarRecaudos[Any]],
) -> None:
    """Con la antigüedad mínima por defecto (5 min): uno de hace 10 min sí, uno de hace 1 min no."""
    antiguo = await pendiente(recaudos, C06)
    reciente = await pendiente(recaudos, C06, hace=timedelta(minutes=1))

    conciliados = await conciliacion().conciliar_una_vez()

    assert [c.clave_recaudo for c in conciliados] == [antiguo.clave_recaudo]
    assert [clave_de(c) for c in adaptador.consultas()] == [str(antiguo.clave_recaudo)]
    estados = movimientos(limpia)
    assert estados[antiguo.clave_recaudo][0] == "CONFIRMADO"
    assert estados[reciente.clave_recaudo][0] == "PENDIENTE"


async def test_el_lote_acota_y_toma_primero_los_mas_antiguos(
    recaudos: RecaudosPg,
    adaptador: Any,
    conciliacion: Callable[..., ConciliarRecaudos[Any]],
) -> None:
    viejos = [await pendiente(recaudos, C06, hace=timedelta(minutes=30 - i)) for i in range(3)]

    conciliados = await conciliacion(conciliacion_lote=2).conciliar_una_vez()

    assert [c.clave_recaudo for c in conciliados] == [v.clave_recaudo for v in viejos[:2]]
    assert len(adaptador.consultas()) == 2


async def test_la_app_concilia_sola_con_la_tarea_del_ciclo_de_vida(
    pool: Any, recaudos: RecaudosPg, limpia: str, adaptador: Any
) -> None:
    """La app real (crear_app) con la conciliación encendida por defecto y un intervalo corto."""
    movimiento = await pendiente(recaudos, C06)
    settings = Settings(ADAPTADOR_PAGOS_URL=adaptador.url, conciliacion_intervalo_s=0.2)
    assert settings.conciliacion_habilitada
    app = crear_app(settings, pool=pool)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if movimientos(limpia)[movimiento.clave_recaudo][0] != "PENDIENTE":
                break
            await asyncio.sleep(0.1)
    assert movimientos(limpia)[movimiento.clave_recaudo][0] == "CONFIRMADO"
    assert [fila[0] for fila in outbox(limpia)] == ["RecaudoConfirmado"]
