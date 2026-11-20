"""MT-W23-01 y MT-W23-02: la conciliación de indemnizaciones (DS3-18) contra PostgreSQL real y un
WireMock en lugar de adaptador-pagos.

- Pasarela lenta → PENDIENTE → la conciliación, con la consulta que confirma, resuelve **la misma
  fila** (AC3), con su evento y sin sumar un intento de pago.
- La pasarela que no conoce la referencia: se reintenta el desembolso con la misma referencia.
- Dos réplicas a la vez (`SKIP LOCKED`): una sola consulta por movimiento.
- Los movimientos que no se pueden reclamar (recientes, resueltos o reclamados) no se consultan.
"""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from dinero.adapters.outbound.indemnizaciones_pg import IndemnizacionesPg
from dinero.adapters.outbound.pagos_http import AdaptadorPagosHttp
from dinero.adapters.outbound.reloj import RelojSistema
from dinero.application.indemnizacion.conciliar import ConciliarIndemnizaciones, Resultado
from dinero.config import Settings
from dinero.domain.indemnizacion.modelos import Indemnizacion
from dinero.main import armar_conciliacion_indemnizaciones
from jsonschema import Draft202012Validator
from solventa_common import correlation

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
TIMEOUT_PRUEBA_MS = 300
BEN_OK = "ben-ok"
BEN_RECHAZA = "ben-rechaza"
BEN_LENTO = "ben-lento"
BEN_REINTENTO = "ben-reintento"
BEN_CONSULTA_LENTA = "ben-consulta-lenta"
BEN_INCIERTO_CONSULTA = "ben-incierto-consulta"


def _ejecutar(url: str, sql: str, parametros: Any = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.execute(sql, parametros)
        return cursor.fetchall() if cursor.description else []


def _validador(tipo: str) -> Draft202012Validator:
    esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


async def _sembrar(
    url: str,
    pool: Any,
    beneficiario: str,
    *,
    antiguedad_min: float = 30,
    incierto: str | None = None,
    codigo: str | None = None,
) -> UUID:
    """Una PENDIENTE creada hace `antiguedad_min` minutos (como la dejaría un timeout)."""
    reloj = RelojSistema().ahora() - timedelta(minutes=antiguedad_min)
    n = codigo or f"{uuid4().int % 9000 + 1000}"
    i = Indemnizacion.pendiente(
        siniestro_id=uuid4(),
        referencia=f"IND-88-2026-{n}-01",
        monto=Decimal("2500000.00"),
        moneda="COP",
        beneficiario_ref=beneficiario,
        correlation_id=f"corr-concilia-{n}",
        ahora=reloj,
    )
    if incierto:
        i = i.registrar_incierto(incierto, reloj)
    await IndemnizacionesPg(pool).registrar_pendiente(i)
    return i.indemnizacion_id


def _filas(url: str) -> dict[UUID, tuple[Any, ...]]:
    consulta = (
        "SELECT indemnizacion_id, estado, transaccion_id, motivo, intentos, ultimo_incierto, "
        "reclamado_hasta, duplicados_descartados FROM indemnizaciones"
    )
    return {f[0]: f[1:] for f in _ejecutar(url, consulta)}


class Conciliaciones:
    def __init__(self, pool: Any, adaptador: Any, cliente: httpx.AsyncClient) -> None:
        self.pool = pool
        self.pagos = AdaptadorPagosHttp(cliente, adaptador.url, TIMEOUT_PRUEBA_MS)

    def __call__(self, lote: int = 10) -> ConciliarIndemnizaciones[Any]:
        settings = Settings(
            indemnizacion_conciliacion_antiguedad_min_s=300,
            indemnizacion_conciliacion_lote=lote,
            indemnizacion_conciliacion_reclamo_s=60,
        )
        return armar_conciliacion_indemnizaciones(self.pool, self.pagos, RelojSistema(), settings)


@pytest.fixture
async def conciliacion_de(pool: Any, adaptador: Any) -> Any:
    async with correlation.cliente_async(timeout=httpx.Timeout(1.0)) as cliente:
        yield Conciliaciones(pool, adaptador, cliente)


# --- AC3: lento y luego confirmado -----------------------------------------------------------------
async def test_lento_y_luego_confirmado_actualiza_la_misma_fila(
    conciliacion_de: Any, pool: Any, limpia: str, adaptador: Any
) -> None:
    """El desembolso lento (ben-lento) dejó la fila PENDIENTE con un incierto; la consulta confirma."""
    ident = await _sembrar(limpia, pool, BEN_LENTO, incierto="ADAPTADOR_TIEMPO_AGOTADO")
    [conciliada] = await conciliacion_de().conciliar_una_vez()
    assert (conciliada.resultado, conciliada.indemnizacion_id) == (Resultado.CONFIRMADA, ident)

    filas = _filas(limpia)
    assert list(filas) == [ident]  # la misma fila: no hay otra
    estado, transaccion, motivo, intentos, incierto, reclamado, duplicados = filas[ident]
    assert (estado, motivo, intentos, incierto) == (
        "CONFIRMADA",
        None,
        1,
        "ADAPTADOR_TIEMPO_AGOTADO",
    )
    assert transaccion.startswith("TRX-IND-88-2026-") and reclamado is None and duplicados == 0
    [(tipo, agregado, payload)] = _ejecutar(
        limpia, "SELECT event_type, aggregate_id, payload FROM outbox"
    )
    assert (tipo, agregado) == ("IndemnizacionConfirmada", ident)
    sobre = {
        "eventId": str(uuid4()),
        "aggregateId": str(agregado),
        "type": tipo,
        "version": 1,
        "occurredAt": "2026-11-10T15:00:03Z",
        "correlationId": "corr",
        "payload": payload,
    }
    assert list(_validador("IndemnizacionConfirmada").iter_errors(sobre)) == []
    assert payload["intentos"] == 1
    # Solo consultó: ninguna llamada de pago.
    assert adaptador.desembolsos() == []
    [consulta] = adaptador.consultas_desembolso()
    assert consulta["queryParams"]["beneficiarioRef"]["values"] == [BEN_LENTO]
    assert consulta["headers"]["X-Correlation-Id"].startswith("corr-concilia-")


async def test_la_pasarela_rechaza_en_la_consulta(
    conciliacion_de: Any, pool: Any, limpia: str, adaptador: Any
) -> None:
    ident = await _sembrar(limpia, pool, BEN_RECHAZA, incierto="ADAPTADOR_TIEMPO_AGOTADO")
    [conciliada] = await conciliacion_de().conciliar_una_vez()
    assert (conciliada.resultado, conciliada.codigo) == (
        Resultado.RECHAZADA,
        "BENEFICIARIO_NO_HABILITADO",
    )
    estado, transaccion, motivo, *_ = _filas(limpia)[ident]
    assert (estado, transaccion, motivo) == ("RECHAZADA", None, "BENEFICIARIO_NO_HABILITADO")
    [(tipo, *_)] = _ejecutar(limpia, "SELECT event_type FROM outbox")
    assert tipo == "IndemnizacionRechazada"


async def test_si_la_pasarela_no_la_conoce_reintenta_con_la_misma_referencia(
    conciliacion_de: Any, pool: Any, limpia: str, adaptador: Any
) -> None:
    ident = await _sembrar(limpia, pool, BEN_REINTENTO, codigo="4501")
    # El primer POST (la continuación) fallaría con 503: se simula con el primero del escenario.
    async with httpx.AsyncClient() as cliente:
        await cliente.post(
            f"{adaptador.url}/v1/desembolsos",
            json={
                "referencia": "IND-88-2026-4501-01",
                "monto": "1",
                "moneda": "COP",
                "beneficiarioRef": BEN_REINTENTO,
            },
        )
    adaptador.limpiar_journal()
    [conciliada] = await conciliacion_de().conciliar_una_vez()
    assert (conciliada.resultado, conciliada.reintento) == (Resultado.CONFIRMADA, True)
    [pago] = adaptador.desembolsos()
    assert json.loads(pago["body"])["referencia"] == "IND-88-2026-4501-01"
    assert len(adaptador.consultas_desembolso()) == 1
    estado, _, _, intentos, *_ = _filas(limpia)[ident]
    assert (estado, intentos) == ("CONFIRMADA", 1)  # el reintento sí es un intento de pago


async def test_un_incierto_de_la_consulta_la_deja_pendiente_y_renueva_su_antiguedad(
    conciliacion_de: Any, pool: Any, limpia: str, adaptador: Any
) -> None:
    ident = await _sembrar(limpia, pool, BEN_INCIERTO_CONSULTA)
    antes = _ejecutar(limpia, "SELECT actualizado_en FROM indemnizaciones")[0][0]
    [conciliada] = await conciliacion_de().conciliar_una_vez()
    assert (
        conciliada.resultado is Resultado.INCIERTO and conciliada.codigo == "FUENTE_NO_DISPONIBLE"
    )
    estado, _, _, intentos, incierto, reclamado, _ = _filas(limpia)[ident]
    assert (estado, intentos, incierto, reclamado) == ("PENDIENTE", 0, "FUENTE_NO_DISPONIBLE", None)
    assert _ejecutar(limpia, "SELECT actualizado_en FROM indemnizaciones")[0][0] > antes
    assert _ejecutar(limpia, "SELECT count(*) FROM outbox") == [(0,)]
    # Recién renovada: el siguiente ciclo no la toma hasta que pase otra antigüedad mínima.
    assert await conciliacion_de().conciliar_una_vez() == []


# --- MT-W23-02: dos réplicas a la vez --------------------------------------------------------------
async def test_dos_tareas_a_la_vez_no_consultan_dos_veces_el_mismo_movimiento(
    conciliacion_de: Any, pool: Any, limpia: str, adaptador: Any
) -> None:
    """La consulta tarda 200 ms (ben-consulta-lenta): ensancha la ventana de la carrera."""
    ids = {
        await _sembrar(
            limpia, pool, BEN_CONSULTA_LENTA, antiguedad_min=60 - n, codigo=f"{4600 + n}"
        )
        for n in range(6)
    }
    primera, segunda = await asyncio.gather(
        conciliacion_de().conciliar_una_vez(), conciliacion_de().conciliar_una_vez()
    )
    tomadas = [c.indemnizacion_id for c in primera + segunda]
    assert len(tomadas) == 6 and set(tomadas) == ids  # cada una, una sola vez
    referencias = [
        c["url"].split("/v1/desembolsos/")[1].split("?")[0]
        for c in adaptador.consultas_desembolso()
    ]
    assert len(referencias) == 6 and len(set(referencias)) == 6  # una consulta por movimiento
    assert all(f[0] == "CONFIRMADA" for f in _filas(limpia).values())
    assert _ejecutar(limpia, "SELECT count(*) FROM outbox") == [(6,)]  # un evento por movimiento
    assert adaptador.desembolsos() == []


async def test_cuatro_tareas_con_lotes_pequenos_reparten_el_trabajo(
    conciliacion_de: Any, pool: Any, limpia: str, adaptador: Any
) -> None:
    for n in range(8):
        await _sembrar(limpia, pool, BEN_OK, antiguedad_min=60 - n, codigo=f"{4700 + n}")
    ciclos = await asyncio.gather(*(conciliacion_de(lote=2).conciliar_una_vez() for _ in range(4)))
    assert sorted(len(c) for c in ciclos) == [2, 2, 2, 2]
    assert len(adaptador.consultas_desembolso()) == 8
    assert all(f[0] == "CONFIRMADA" for f in _filas(limpia).values())


# --- Lo que no se reclama -------------------------------------------------------------------------
async def test_no_consulta_lo_reciente_lo_resuelto_ni_lo_reclamado(
    conciliacion_de: Any, pool: Any, limpia: str, adaptador: Any
) -> None:
    await _sembrar(limpia, pool, BEN_OK, antiguedad_min=2)  # reciente
    resuelta = await _sembrar(limpia, pool, BEN_OK)
    _ejecutar(
        limpia,
        "UPDATE indemnizaciones SET estado = 'CONFIRMADA', transaccion_id = 'T', "
        "resuelto_en = now(), intentos = 1 WHERE indemnizacion_id = %s",
        (resuelta,),
    )
    reclamada = await _sembrar(limpia, pool, BEN_OK)
    _ejecutar(
        limpia,
        "UPDATE indemnizaciones SET reclamado_hasta = now() + interval '30 seconds' "
        "WHERE indemnizacion_id = %s",
        (reclamada,),
    )
    assert await conciliacion_de().conciliar_una_vez() == []
    assert adaptador.consultas_desembolso() == []


async def test_el_reclamo_vencido_se_puede_tomar(
    conciliacion_de: Any, pool: Any, limpia: str, adaptador: Any
) -> None:
    ident = await _sembrar(limpia, pool, BEN_OK)
    _ejecutar(
        limpia,
        "UPDATE indemnizaciones SET reclamado_hasta = now() - interval '1 second' "
        "WHERE indemnizacion_id = %s",
        (ident,),
    )
    [conciliada] = await conciliacion_de().conciliar_una_vez()
    assert conciliada.resultado is Resultado.CONFIRMADA


async def test_los_logs_de_la_conciliacion_no_llevan_el_beneficiario(
    conciliacion_de: Any, pool: Any, limpia: str, adaptador: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    await _sembrar(limpia, pool, BEN_OK)
    await conciliacion_de().conciliar_una_vez()
    texto = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert "indemnizacion conciliada" in texto and BEN_OK not in texto
