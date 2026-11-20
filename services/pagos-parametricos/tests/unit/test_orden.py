"""UT-W21-03 (N1): la regla de orden de DS3-22: `aggregateVersion` si viene (mayor, igual, menor)
y `occurredAt` como respaldo (posterior, igual, anterior)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import permutations
from typing import Any
from uuid import UUID

import pytest
from pagos_parametricos.domain.parametrico.modelos import Cobertura, EstadoCobertura, Regla
from pagos_parametricos.domain.parametrico.orden import (
    EventoPoliza,
    PolizaCancelada,
    PolizaEmitida,
    aplicar_evento_poliza,
    avanza_orden,
)

POLIZA = UUID(int=7)
T0 = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
REGLA = Regla(
    "LLUVIA_ACUMULADA",
    "11001",
    Decimal(120),
    "mm",
    Decimal("1400000.00"),
    "COP",
    "1",
    T0,
    T0 + timedelta(days=365),
)


def _emitida(en: datetime = T0, version: int | None = None, regla: Regla = REGLA) -> PolizaEmitida:
    return PolizaEmitida(POLIZA, "250-2026-000101", regla, en, version)


def _cancelada(en: datetime = T0, version: int | None = None) -> PolizaCancelada:
    return PolizaCancelada(POLIZA, en, version)


def _estado(*eventos: EventoPoliza) -> Cobertura | None:
    actual: Cobertura | None = None
    for evento in eventos:
        actual = aplicar_evento_poliza(actual, evento) or actual
    return actual


# --- Sin estado previo ---------------------------------------------------------------------------
def test_la_primera_emision_crea_la_cobertura_activa() -> None:
    cobertura = aplicar_evento_poliza(None, _emitida(version=1))
    assert cobertura is not None
    assert (cobertura.estado, cobertura.regla, cobertura.aggregate_version) == (
        EstadoCobertura.ACTIVA,
        REGLA,
        1,
    )
    assert (cobertura.numero, cobertura.ultimo_evento_en) == ("250-2026-000101", T0)


def test_una_cancelacion_sin_emision_previa_deja_la_marca_sin_regla() -> None:
    cobertura = aplicar_evento_poliza(None, _cancelada(T0 + timedelta(hours=1)))
    assert cobertura is not None
    assert (cobertura.estado, cobertura.regla, cobertura.numero) == (
        EstadoCobertura.CANCELADA,
        None,
        None,
    )


def test_la_cancelacion_conserva_lo_que_se_sabia_de_la_poliza() -> None:
    actual = aplicar_evento_poliza(None, _emitida())
    cobertura = aplicar_evento_poliza(actual, _cancelada(T0 + timedelta(seconds=1)))
    assert cobertura is not None
    assert (cobertura.estado, cobertura.regla, cobertura.numero) == (
        EstadoCobertura.CANCELADA,
        REGLA,
        "250-2026-000101",
    )


# --- aggregateVersion: mayor, igual, menor -------------------------------------------------------
@pytest.mark.parametrize(("nueva", "avanza"), [(3, True), (2, False), (1, False)])
def test_con_aggregate_version_solo_avanza_una_version_mayor(nueva: int, avanza: bool) -> None:
    actual = aplicar_evento_poliza(None, _emitida(version=2))
    evento = _cancelada(T0 + timedelta(days=1), version=nueva)  # el instante no importa
    assert avanza_orden(actual, evento) is avanza
    assert (aplicar_evento_poliza(actual, evento) is not None) is avanza


def test_la_version_pesa_mas_que_el_instante() -> None:
    """Una cancelación de versión 1 posterior en el tiempo no cancela una emisión de versión 2."""
    actual = aplicar_evento_poliza(None, _emitida(version=2))
    assert aplicar_evento_poliza(actual, _cancelada(T0 + timedelta(days=9), version=1)) is None
    # Y una versión mayor gana aunque su instante sea anterior.
    cancelada = aplicar_evento_poliza(actual, _cancelada(T0 - timedelta(days=9), version=3))
    assert cancelada is not None and cancelada.estado is EstadoCobertura.CANCELADA


# --- occurredAt como respaldo: posterior, igual, anterior ----------------------------------------
@pytest.mark.parametrize(
    ("desfase", "avanza"),
    [
        (timedelta(seconds=1), True),
        (timedelta(microseconds=1), True),
        (timedelta(0), False),
        (-timedelta(microseconds=1), False),
        (-timedelta(days=1), False),
    ],
)
def test_sin_version_avanza_solo_un_instante_posterior(desfase: timedelta, avanza: bool) -> None:
    actual = aplicar_evento_poliza(None, _emitida())
    assert avanza_orden(actual, _cancelada(T0 + desfase)) is avanza


def test_un_empate_no_cambia_el_estado() -> None:
    actual = aplicar_evento_poliza(None, _emitida(T0))
    assert aplicar_evento_poliza(actual, _cancelada(T0)) is None
    assert _estado(_emitida(T0), _cancelada(T0)) == actual


def test_si_uno_trae_version_y_el_otro_no_se_comparan_los_instantes() -> None:
    con_version = aplicar_evento_poliza(None, _emitida(T0, version=5))
    assert aplicar_evento_poliza(con_version, _cancelada(T0 - timedelta(seconds=1))) is None
    assert aplicar_evento_poliza(con_version, _cancelada(T0 + timedelta(seconds=1))) is not None
    sin_version = aplicar_evento_poliza(None, _emitida(T0))
    assert aplicar_evento_poliza(sin_version, _cancelada(T0 - timedelta(seconds=1), 9)) is None
    assert aplicar_evento_poliza(sin_version, _cancelada(T0 + timedelta(seconds=1), 1)) is not None


def test_el_instante_con_otra_zona_horaria_se_compara_en_utc() -> None:
    bogota = datetime.fromisoformat("2026-11-10T05:00:00-05:00")  # = T0
    actual = aplicar_evento_poliza(None, _emitida(T0))
    assert aplicar_evento_poliza(actual, _cancelada(bogota)) is None


# --- Permutaciones: el estado final no depende del orden de llegada ----------------------------
@pytest.mark.parametrize("con_version", [False, True])
def test_el_estado_final_es_el_mismo_con_cualquier_orden_de_llegada(con_version: bool) -> None:
    eventos: list[EventoPoliza] = [
        _emitida(T0, 1 if con_version else None),
        _cancelada(T0 + timedelta(hours=1), 2 if con_version else None),
        _emitida(T0 + timedelta(hours=2), 3 if con_version else None, REGLA),
    ]
    finales = {(c.estado, c.ultimo_evento_en) for p in permutations(eventos) if (c := _estado(*p))}
    # La reemisión más reciente gana siempre: la póliza queda activa con el último evento.
    assert finales == {(EstadoCobertura.ACTIVA, T0 + timedelta(hours=2))}


def test_una_emision_atrasada_no_reactiva_una_cancelada() -> None:
    cancelada = _estado(_cancelada(T0 + timedelta(hours=1)))
    assert cancelada is not None
    assert aplicar_evento_poliza(cancelada, _emitida(T0)) is None


def test_una_reemision_posterior_reactiva_con_la_regla_nueva() -> None:
    nueva = Regla(
        "LLUVIA_ACUMULADA",
        "05001",
        Decimal(100),
        "mm",
        Decimal(900000),
        "COP",
        "2",
        T0,
        T0 + timedelta(days=30),
    )
    cobertura = _estado(
        _emitida(T0),
        _cancelada(T0 + timedelta(hours=1)),
        _emitida(T0 + timedelta(hours=2), regla=nueva),
    )
    assert cobertura is not None
    assert (cobertura.estado, cobertura.regla) == (EstadoCobertura.ACTIVA, nueva)


# --- Los cinco escenarios del archivo de datos, con el dominio puro ------------------------------
def test_los_escenarios_de_orden_del_archivo_de_datos_dan_el_estado_esperado(
    datos: dict[str, Any],
) -> None:
    sobres: dict[str, dict[str, Any]] = {
        c["clave"]: c["sobre"] for c in [*datos["coberturas"], *datos["cancelaciones"]]
    }

    def a_evento(sobre: dict[str, Any]) -> EventoPoliza:
        ocurrido = datetime.fromisoformat(sobre["occurredAt"])
        version = sobre.get("aggregateVersion")
        poliza = UUID(sobre["aggregateId"])
        if sobre["type"] == "PolizaCancelada":
            return PolizaCancelada(poliza, ocurrido, version)
        return PolizaEmitida(poliza, sobre["payload"]["numero"], REGLA, ocurrido, version)

    assert len(datos["ordenCoberturas"]) == 5
    for escenario in datos["ordenCoberturas"]:
        cobertura = _estado(*(a_evento(sobres[clave]) for clave in escenario["llegada"]))
        assert cobertura is not None
        assert cobertura.estado.value == escenario["esperado"]["estado"], escenario["clave"]
