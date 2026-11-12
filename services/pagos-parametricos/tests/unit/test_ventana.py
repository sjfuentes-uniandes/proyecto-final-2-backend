"""La ventana de una cobertura es la vigencia inclusiva de la póliza, en UTC−5 fijo (R-150)."""

from datetime import UTC, date, datetime

import pytest
from pagos_parametricos.domain.parametrico.ventana import ventana_de_vigencia


def test_la_vigencia_de_un_ano_cubre_desde_la_medianoche_de_colombia_hasta_la_del_dia_siguiente() -> (
    None
):
    assert ventana_de_vigencia(date(2026, 11, 2), date(2027, 11, 1)) == (
        datetime(2026, 11, 2, 5, tzinfo=UTC),
        datetime(2027, 11, 2, 5, tzinfo=UTC),
    )


def test_un_solo_dia_de_vigencia_dura_veinticuatro_horas() -> None:
    inicio, fin = ventana_de_vigencia(date(2026, 11, 2), date(2026, 11, 2))
    assert (fin - inicio).total_seconds() == 86400


def test_el_fin_de_ano_y_los_bisiestos_no_rompen_la_cuenta() -> None:
    assert ventana_de_vigencia(date(2027, 12, 31), date(2027, 12, 31))[1] == datetime(
        2028, 1, 1, 5, tzinfo=UTC
    )
    assert ventana_de_vigencia(date(2028, 2, 28), date(2028, 2, 28))[1] == datetime(
        2028, 2, 29, 5, tzinfo=UTC
    )


def test_una_vigencia_que_termina_antes_de_empezar_se_rechaza() -> None:
    with pytest.raises(ValueError):
        ventana_de_vigencia(date(2026, 11, 2), date(2026, 11, 1))
