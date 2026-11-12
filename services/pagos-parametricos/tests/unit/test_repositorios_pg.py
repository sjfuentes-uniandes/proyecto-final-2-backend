"""`CoberturasPg` y `EvaluacionesPg` con una conexión falsa (sin base): las sentencias, el
bloqueo por póliza, el lote de evaluaciones y la fila del Outbox. El comportamiento contra
PostgreSQL real está en `tests/module`."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self
from uuid import UUID

import pytest
from pagos_parametricos.adapters.outbound.coberturas_pg import CoberturasPg, cobertura_de
from pagos_parametricos.adapters.outbound.evaluaciones_pg import EvaluacionesPg, sobre_de
from pagos_parametricos.domain.parametrico.evaluacion import evaluar
from pagos_parametricos.domain.parametrico.modelos import (
    Cobertura,
    EstadoCobertura,
    EventoObservado,
    Regla,
)

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 11, 10, 14, 0, 6, tzinfo=UTC)
POLIZA = UUID(int=1)
EVENTO = UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09")
REGLA = Regla(
    "LLUVIA_ACUMULADA",
    "11001",
    Decimal(120),
    "mm",
    Decimal("1400000.00"),
    "COP",
    "1",
    datetime(2026, 9, 1, 5, tzinfo=UTC),
    datetime(2027, 9, 1, 5, tzinfo=UTC),
)
FILA_ACTIVA = (
    POLIZA, "250-2026-000101", "ACTIVA", "LLUVIA_ACUMULADA", "11001", Decimal(120), "mm",
    Decimal("1400000.00"), "COP", "1", REGLA.vigente_desde, REGLA.vigente_hasta, 2,
    datetime(2026, 11, 1, tzinfo=UTC),
)  # fmt: skip
FILA_CANCELADA_SIN_REGLA = (
    POLIZA, None, "CANCELADA", None, None, None, None, None, None, None, None, None, None,
    datetime(2026, 11, 1, tzinfo=UTC),
)  # fmt: skip


class Cursor:
    def __init__(self, conexion: "Conexion", filas: list[tuple[Any, ...]]) -> None:
        self.conexion = conexion
        self.filas = filas

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.filas[0] if self.filas else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self.filas

    async def executemany(self, sql: str, lotes: list[tuple[Any, ...]]) -> None:
        self.conexion.lotes.append((sql, lotes))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class Conexion:
    def __init__(self, *respuestas: list[tuple[Any, ...]]) -> None:
        self.respuestas = list(respuestas)
        self.sentencias: list[tuple[str, tuple[Any, ...] | None]] = []
        self.lotes: list[tuple[str, list[tuple[Any, ...]]]] = []

    async def execute(self, sql: str, parametros: tuple[Any, ...] | None = None) -> Cursor:
        self.sentencias.append((sql, parametros))
        return Cursor(self, self.respuestas.pop(0) if self.respuestas else [])

    def cursor(self) -> Cursor:
        return Cursor(self, [])


class Tx:
    def __init__(self, conexion: Conexion) -> None:
        self.conexion = conexion


def _evento(**cambios: Any) -> EventoObservado:
    base: dict[str, Any] = {
        "evento_id": EVENTO,
        "fuente": "IDEAM-SIM",
        "id_externo": "IDEAM-2026-1110-0001",
        "evento_origen": "EVT-2026-0913",
        "tipo_evento": "LLUVIA_ACUMULADA",
        "valor_observado": Decimal(135),
        "unidad": "mm",
        "zona": "11001",
        "observado_en": datetime(2026, 11, 10, 14, tzinfo=UTC),
    }
    base.update(cambios)
    return EventoObservado(**base)


# --- Coberturas ----------------------------------------------------------------------------------
def test_la_fila_activa_se_lee_con_su_regla_y_la_cancelada_sin_regla_tambien() -> None:
    activa = cobertura_de(FILA_ACTIVA)
    assert activa.estado is EstadoCobertura.ACTIVA and activa.regla == REGLA
    assert (activa.aggregate_version, activa.numero) == (2, "250-2026-000101")
    cancelada = cobertura_de(FILA_CANCELADA_SIN_REGLA)
    assert (cancelada.estado, cancelada.regla, cancelada.numero) == (
        EstadoCobertura.CANCELADA,
        None,
        None,
    )


async def test_leer_bloquea_la_poliza_antes_de_leerla() -> None:
    conexion = Conexion([], [FILA_ACTIVA])
    cobertura = await CoberturasPg().bloquear_y_leer(Tx(conexion), POLIZA)  # type: ignore[arg-type]
    assert cobertura is not None and cobertura.poliza_id == POLIZA
    bloqueo, lectura = conexion.sentencias
    assert "pg_advisory_xact_lock" in bloqueo[0] and bloqueo[1] == (str(POLIZA),)
    assert lectura[0].startswith("SELECT") and lectura[1] == (POLIZA,)


async def test_leer_sin_bloquear_y_sin_fila() -> None:
    conexion = Conexion([FILA_ACTIVA], [])
    repo = CoberturasPg()
    assert await repo.leer(Tx(conexion), POLIZA) is not None  # type: ignore[arg-type]
    assert await repo.leer(Tx(conexion), POLIZA) is None  # type: ignore[arg-type]
    assert not any("advisory" in s for s, _ in conexion.sentencias)


async def test_guardar_es_un_upsert_con_todas_las_columnas_de_la_regla() -> None:
    conexion = Conexion()
    cobertura = cobertura_de(FILA_ACTIVA)
    await CoberturasPg().guardar(Tx(conexion), cobertura)  # type: ignore[arg-type]
    [(sql, parametros)] = conexion.sentencias
    assert "ON CONFLICT (poliza_id) DO UPDATE" in sql
    assert parametros is not None and len(parametros) == 14
    assert parametros[:3] == (POLIZA, "250-2026-000101", "ACTIVA")
    assert parametros[3:10] == (
        "LLUVIA_ACUMULADA", "11001", Decimal(120), "mm", Decimal("1400000.00"), "COP", "1",
    )  # fmt: skip
    assert parametros[12:] == (2, datetime(2026, 11, 1, tzinfo=UTC))


async def test_guardar_una_cancelada_sin_regla_escribe_nulos_en_la_regla() -> None:
    conexion = Conexion()
    await CoberturasPg().guardar(Tx(conexion), cobertura_de(FILA_CANCELADA_SIN_REGLA))  # type: ignore[arg-type]
    parametros = conexion.sentencias[0][1]
    assert parametros is not None
    assert parametros[3:12] == (None,) * 9 and parametros[2] == "CANCELADA"


async def test_las_candidatas_salen_del_indice_de_tipo_y_zona_solo_las_activas() -> None:
    conexion = Conexion([FILA_ACTIVA])
    candidatas = await CoberturasPg().candidatas(Tx(conexion), "LLUVIA_ACUMULADA", "11001")  # type: ignore[arg-type]
    assert [c.poliza_id for c in candidatas] == [POLIZA]
    sql, parametros = conexion.sentencias[0]
    assert "estado = 'ACTIVA'" in sql and "tipo_evento = %s AND zona = %s" in sql
    assert parametros == ("LLUVIA_ACUMULADA", "11001")


# --- Evaluaciones --------------------------------------------------------------------------------
async def test_registrar_escribe_el_evento_las_evaluaciones_las_activaciones_y_el_outbox() -> None:
    # Una póliza que activa y otra con un umbral más alto que no.
    exigente = Regla(
        "LLUVIA_ACUMULADA", "11001", Decimal(200), "mm", Decimal(500000), "COP", "3",
        REGLA.vigente_desde, REGLA.vigente_hasta,
    )  # fmt: skip
    resultado = evaluar(
        _evento(),
        [
            cobertura_de(FILA_ACTIVA),
            Cobertura(UUID(int=2), EstadoCobertura.ACTIVA, exigente, AHORA),
        ],
    )
    conexion = Conexion()
    await EvaluacionesPg().registrar(Tx(conexion), _evento(), resultado, AHORA, "corr-9")  # type: ignore[arg-type]
    [evento, outbox] = conexion.sentencias
    assert evento[0].startswith("INSERT INTO eventos_evaluados")
    assert evento[1] == (
        EVENTO, "IDEAM-SIM", "IDEAM-2026-1110-0001", "EVT-2026-0913", "ACTIVA", None, 2, 1, "1", AHORA,
    )  # fmt: skip
    [(sql_e, evaluaciones), (sql_a, activaciones)] = conexion.lotes
    assert sql_e.startswith("INSERT INTO evaluaciones")
    assert [(e[1], e[2], e[3], e[4]) for e in evaluaciones] == [
        (UUID(int=1), "ACTIVA", None, "1"),
        (UUID(int=2), "NO_ACTIVA", "BAJO_UMBRAL", "3"),
    ]
    assert "'PENDIENTE'" in sql_a
    # Solo la que activó, con lo que W22 necesita tal como estaba al evaluar.
    assert activaciones == [(EVENTO, UUID(int=1), Decimal("1400000.00"), "COP", "1", AHORA)]
    assert outbox[0].startswith("INSERT INTO outbox")


async def test_un_evento_sin_candidatas_escribe_una_sola_fila_y_ningun_lote() -> None:
    conexion = Conexion()
    await EvaluacionesPg().registrar(Tx(conexion), _evento(), evaluar(_evento(), []), AHORA, "c")  # type: ignore[arg-type]
    assert [s.split()[2] for s, _ in conexion.sentencias] == ["eventos_evaluados", "outbox"]
    assert conexion.sentencias[0][1][4:6] == ("SIN_POLIZAS", "SIN_POLIZAS_EN_ZONA")  # type: ignore[index]
    assert conexion.lotes == []


async def test_si_ninguna_activo_no_hay_activaciones() -> None:
    conexion = Conexion()
    resultado = evaluar(_evento(valor_observado=Decimal(1)), [cobertura_de(FILA_ACTIVA)])
    await EvaluacionesPg().registrar(Tx(conexion), _evento(), resultado, AHORA, "c")  # type: ignore[arg-type]
    assert [s.split()[2] for s, _ in conexion.lotes] == ["evaluaciones"]


async def test_descartar_duplicado_suma_uno_solo_si_la_fila_existe() -> None:
    conexion = Conexion([(EVENTO,)], [])
    repo = EvaluacionesPg()
    assert await repo.descartar_duplicado(Tx(conexion), EVENTO) is True  # type: ignore[arg-type]
    assert await repo.descartar_duplicado(Tx(conexion), EVENTO) is False  # type: ignore[arg-type]
    assert "duplicados = duplicados + 1" in conexion.sentencias[0][0]


# --- El sobre ------------------------------------------------------------------------------------
def test_el_sobre_del_evento_evaluado_tiene_una_identidad_determinista() -> None:
    resultado = evaluar(_evento(), [cobertura_de(FILA_ACTIVA)])
    a = sobre_de(_evento(), resultado, AHORA, "corr")
    b = sobre_de(_evento(), resultado, AHORA, "corr")
    assert a.event_id == b.event_id != EVENTO
    assert a.aggregate_id == a.payload.evento_id == EVENTO
    assert (a.type, a.version, a.occurred_at, a.correlation_id) == (
        "EventoParametricoEvaluado",
        1,
        AHORA,
        "corr",
    )
    payload = a.payload.model_dump(mode="json", by_alias=True)
    assert payload["decision"] == "ACTIVA" and payload["razon"] is None
    assert (payload["activaciones"], payload["evaluadas"], payload["versionRegla"]) == (1, 1, "1")
    # El origen se conserva y no se copian valores ni pólizas.
    assert (payload["fuente"], payload["idExterno"]) == ("IDEAM-SIM", "IDEAM-2026-1110-0001")
    assert "valorObservado" not in payload and "polizaId" not in str(payload)


def test_la_razon_de_sin_polizas_sale_en_el_evento() -> None:
    payload = sobre_de(_evento(), evaluar(_evento(), []), AHORA, "c").payload
    assert payload.model_dump(mode="json", by_alias=True)["razon"] == "SIN_POLIZAS_EN_ZONA"
