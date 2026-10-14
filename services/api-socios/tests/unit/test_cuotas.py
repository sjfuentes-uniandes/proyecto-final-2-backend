from datetime import UTC, date, datetime

import pytest
from api_socios.domain.cuotas import (
    EstadoCuota,
    LimitesPlan,
    Periodo,
    PlanNoDisponible,
    Ventana,
    calcular_consumo,
    plan_de_uso,
    planes_disponibles,
    ventana,
)
from api_socios.domain.socios import Plan
from hypothesis import given
from hypothesis import strategies as st

PLANES = {"basico": "up-basico", "estandar": "up-estandar", "prueba": "up-prueba"}
PRUEBA = LimitesPlan(Plan.PRUEBA, 1, 2, 5, Periodo.DIA)
HOY = date(2026, 10, 13)


def test_cada_plan_se_mapea_a_su_plan_de_uso() -> None:
    assert {plan: plan_de_uso(PLANES, plan) for plan in Plan} == {
        Plan.BASICO: "up-basico",
        Plan.ESTANDAR: "up-estandar",
        Plan.PRUEBA: "up-prueba",
    }


def test_plan_sin_plan_de_uso_en_el_ambiente_no_esta_disponible() -> None:
    sin_prueba = {"basico": "up-basico", "estandar": "up-estandar"}
    with pytest.raises(PlanNoDisponible):
        plan_de_uso(sin_prueba, Plan.PRUEBA)
    assert planes_disponibles(sin_prueba) == [Plan.BASICO, Plan.ESTANDAR]
    assert planes_disponibles({"desconocido": "x"}) == []


@pytest.mark.parametrize(
    "periodo, desplazamiento, hoy, inicio, fin",
    [
        (Periodo.DIA, 0, HOY, HOY, HOY),
        (Periodo.SEMANA, 0, date(2026, 10, 13), date(2026, 10, 11), date(2026, 10, 17)),
        (Periodo.SEMANA, 0, date(2026, 10, 11), date(2026, 10, 11), date(2026, 10, 17)),
        (Periodo.SEMANA, 1, date(2026, 10, 11), date(2026, 10, 5), date(2026, 10, 11)),
        (Periodo.MES, 0, date(2026, 2, 14), date(2026, 2, 1), date(2026, 2, 28)),
        (Periodo.MES, 0, date(2028, 2, 29), date(2028, 2, 1), date(2028, 2, 29)),
        (Periodo.MES, 9, date(2026, 10, 13), date(2026, 10, 10), date(2026, 11, 9)),
        (Periodo.MES, 14, date(2026, 1, 3), date(2025, 12, 15), date(2026, 1, 14)),
    ],
)
def test_ventana_sigue_el_periodo_y_el_desplazamiento_de_api_gateway(
    periodo: Periodo, desplazamiento: int, hoy: date, inicio: date, fin: date
) -> None:
    vigente = ventana(periodo, hoy, desplazamiento)
    assert (vigente.inicio, vigente.fin) == (inicio, fin)
    assert vigente.inicio <= hoy <= vigente.fin


def test_la_cuota_diaria_se_reinicia_a_medianoche_utc() -> None:
    assert ventana(Periodo.DIA, HOY).reinicio == datetime(2026, 10, 14, tzinfo=UTC)


def test_sin_trafico_queda_toda_la_cuota() -> None:
    consumo = calcular_consumo(PRUEBA, Ventana(HOY, HOY), {}, "key-a")
    assert (consumo.usado, consumo.restante, consumo.porcentaje) == (0, 5, 0)
    assert consumo.estado is EstadoCuota.NORMAL and consumo.limitadas is None


def test_suma_los_dias_y_toma_el_restante_del_ultimo() -> None:
    semanal = LimitesPlan(Plan.BASICO, 5, 10, 1000, Periodo.SEMANA)
    registros = {"key-a": [[100, 900], [250, 650], [0, 650]]}
    consumo = calcular_consumo(semanal, ventana(Periodo.SEMANA, HOY), registros, "key-a", 3)
    assert (consumo.usado, consumo.restante, consumo.porcentaje, consumo.limitadas) == (
        350,
        650,
        35,
        3,
    )


@pytest.mark.parametrize(
    "usado, restante, estado, porcentaje",
    [
        (3, 2, EstadoCuota.NORMAL, 60),
        (4, 1, EstadoCuota.CERCA_DEL_LIMITE, 80),
        (5, 0, EstadoCuota.AGOTADA, 100),
        (7, -2, EstadoCuota.AGOTADA, 100),
    ],
)
def test_estado_de_la_cuota(
    usado: int, restante: int, estado: EstadoCuota, porcentaje: int
) -> None:
    consumo = calcular_consumo(PRUEBA, Ventana(HOY, HOY), {"k": [[usado, restante]]}, "k")
    assert (consumo.estado, consumo.porcentaje, consumo.restante) == (
        estado,
        porcentaje,
        max(0, restante),
    )


def test_una_ampliacion_temporal_se_refleja_en_el_restante() -> None:
    consumo = calcular_consumo(PRUEBA, Ventana(HOY, HOY), {"k": [[5, 10]]}, "k")
    assert (consumo.restante, consumo.estado) == (10, EstadoCuota.CERCA_DEL_LIMITE)


def test_los_contadores_de_dos_api_keys_son_independientes() -> None:
    registros = {"key-a": [[5, 0]], "key-b": [[1, 4]]}
    a = calcular_consumo(PRUEBA, Ventana(HOY, HOY), registros, "key-a")
    b = calcular_consumo(PRUEBA, Ventana(HOY, HOY), registros, "key-b")
    assert (a.usado, a.restante, a.estado) == (5, 0, EstadoCuota.AGOTADA)
    assert (b.usado, b.restante, b.estado) == (1, 4, EstadoCuota.NORMAL)


@given(
    propio=st.lists(st.integers(0, 5), max_size=7),
    ajeno=st.lists(st.tuples(st.integers(0, 10_000), st.integers(0, 10_000)), max_size=7),
)
def test_el_trafico_de_otra_api_key_nunca_cambia_el_consumo_propio(
    propio: list[int], ajeno: list[tuple[int, int]]
) -> None:
    registros_b = {"key-b": [[usado, max(0, 5 - usado)] for usado in propio]}
    solo_b = calcular_consumo(PRUEBA, Ventana(HOY, HOY), registros_b, "key-b")
    con_a = calcular_consumo(
        PRUEBA,
        Ventana(HOY, HOY),
        registros_b | {"key-a": [list(dia) for dia in ajeno]},
        "key-b",
    )
    assert con_a == solo_b
