from datetime import UTC, date, datetime

import pytest
from consulta.domain.polizas.estado import EstadoVigencia, estado_de
from consulta.domain.polizas.modelos import Vigencia, hoy_en_bogota

VIGENCIA = Vigencia(date(2026, 10, 1), date(2026, 12, 31))
N = 30


@pytest.mark.parametrize(
    ("hoy", "esperado"),
    [
        (date(2026, 9, 30), EstadoVigencia.NO_INICIADA),
        (date(2026, 10, 1), EstadoVigencia.VIGENTE),  # el día de inicio
        (date(2026, 11, 30), EstadoVigencia.VIGENTE),  # faltan 31 días: N + 1
        (date(2026, 12, 1), EstadoVigencia.POR_VENCER),  # faltan 30 días: N exactos
        (date(2026, 12, 31), EstadoVigencia.POR_VENCER),  # el último día
        (date(2027, 1, 1), EstadoVigencia.VENCIDA),  # el día siguiente al fin
    ],
)
def test_estado_en_los_bordes(hoy: date, esperado: EstadoVigencia) -> None:
    assert estado_de(VIGENCIA, hoy, N) is esperado


def test_vigencia_de_un_solo_dia_es_por_vencer() -> None:
    dia = date(2026, 10, 25)
    assert estado_de(Vigencia(dia, dia), dia, N) is EstadoVigencia.POR_VENCER


def test_con_cero_dias_solo_el_ultimo_dia_es_por_vencer() -> None:
    assert estado_de(VIGENCIA, date(2026, 12, 30), 0) is EstadoVigencia.VIGENTE
    assert estado_de(VIGENCIA, date(2026, 12, 31), 0) is EstadoVigencia.POR_VENCER


@pytest.mark.parametrize(
    ("instante", "esperado"),
    [
        # 04:59:59 UTC es 23:59:59 del 30 de septiembre en Bogotá; 05:00:00 UTC ya es el 1 de octubre.
        (datetime(2026, 10, 1, 4, 59, 59, tzinfo=UTC), EstadoVigencia.NO_INICIADA),
        (datetime(2026, 10, 1, 5, 0, 0, tzinfo=UTC), EstadoVigencia.VIGENTE),
        # 04:59:59 UTC del 1 de enero es aún el 31 de diciembre en Bogotá.
        (datetime(2027, 1, 1, 4, 59, 59, tzinfo=UTC), EstadoVigencia.POR_VENCER),
        (datetime(2027, 1, 1, 5, 0, 0, tzinfo=UTC), EstadoVigencia.VENCIDA),
    ],
)
def test_el_cambio_de_dia_es_la_medianoche_de_bogota(
    instante: datetime, esperado: EstadoVigencia
) -> None:
    assert estado_de(VIGENCIA, hoy_en_bogota(instante), N) is esperado
