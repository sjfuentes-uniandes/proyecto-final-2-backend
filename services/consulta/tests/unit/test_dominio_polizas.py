from datetime import UTC, date, datetime

import pytest
from consulta.domain.polizas.modelos import Vigencia, es_vigente, hoy_en_bogota

VIGENCIA = Vigencia(date(2026, 10, 25), date(2027, 10, 24))


@pytest.mark.parametrize(
    ("hoy", "esperado"),
    [
        (date(2026, 10, 24), False),
        (date(2026, 10, 25), True),  # fechas inclusivas: el primer día
        (date(2027, 3, 1), True),
        (date(2027, 10, 24), True),  # y el último
        (date(2027, 10, 25), False),
    ],
)
def test_es_vigente_con_fechas_inclusivas(hoy: date, esperado: bool) -> None:
    assert es_vigente(VIGENCIA, hoy) is esperado


def test_hoy_en_bogota_no_es_el_dia_utc_de_madrugada() -> None:
    # 03:00 UTC del 25 son las 22:00 del 24 en Bogotá (UTC-5).
    assert hoy_en_bogota(datetime(2026, 10, 25, 3, 0, tzinfo=UTC)) == date(2026, 10, 24)
    assert hoy_en_bogota(datetime(2026, 10, 25, 5, 0, tzinfo=UTC)) == date(2026, 10, 25)
