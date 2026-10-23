"""Vigencia de la oferta (HU-W13, DS2-20): función pura en los límites y con otro parámetro."""

from datetime import UTC, datetime, timedelta

import pytest
from cotizacion.config import Settings
from cotizacion.domain.cotizacion.vigencia import (
    VIGENCIA_HORAS_POR_OMISION,
    oferta_vigente,
    vigente_hasta,
)

CALCULADO = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
HASTA = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)  # + 96 h
MICRO = timedelta(microseconds=1)


def test_vence_96_horas_despues_del_calculo() -> None:
    assert VIGENCIA_HORAS_POR_OMISION == 96
    assert vigente_hasta(CALCULADO, 96) == HASTA


@pytest.mark.parametrize(
    ("ahora", "vigente"),
    [(HASTA - MICRO, True), (HASTA, False), (HASTA + MICRO, False)],
)
def test_limites_semiabiertos(ahora: datetime, vigente: bool) -> None:
    """−1 µs vigente; el instante exacto y +1 µs, vencida."""
    assert oferta_vigente(vigente_hasta(CALCULADO, 96), ahora) is vigente


@pytest.mark.parametrize(
    ("horas", "hasta"),
    [
        (24, datetime(2026, 10, 23, 14, 0, tzinfo=UTC)),
        (1, datetime(2026, 10, 22, 15, 0, tzinfo=UTC)),
        (0, CALCULADO),
    ],
)
def test_otro_valor_del_parametro(horas: int, hasta: datetime) -> None:
    assert vigente_hasta(CALCULADO, horas) == hasta
    assert oferta_vigente(hasta, hasta - MICRO) is True
    assert oferta_vigente(hasta, hasta) is False


def test_con_cero_horas_nace_vencida() -> None:
    assert oferta_vigente(vigente_hasta(CALCULADO, 0), CALCULADO) is False


def test_es_determinista_y_respeta_la_zona() -> None:
    bogota = CALCULADO.astimezone(datetime(2026, 1, 1).astimezone().tzinfo)
    assert vigente_hasta(bogota, 96) == vigente_hasta(CALCULADO, 96) == HASTA


def test_parametro_por_omision_y_por_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().oferta_vigencia_horas == 96
    monkeypatch.setenv("SOLVENTA_OFERTA_VIGENCIA_HORAS", "24")
    assert Settings().oferta_vigencia_horas == 24
    with pytest.raises(ValueError):
        Settings(oferta_vigencia_horas=-1)
