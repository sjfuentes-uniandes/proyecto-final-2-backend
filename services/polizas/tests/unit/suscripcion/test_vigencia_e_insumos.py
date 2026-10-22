"""UT-W13-02: vigencia de la oferta en los límites (DS2-10) y forma canónica de los insumos."""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from polizas.domain.suscripcion.insumos import hash_insumos, insumos_canonicos, insumos_de
from polizas.domain.suscripcion.vigencia import oferta_vigente

VIGENTE_HASTA = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
MICRO = timedelta(microseconds=1)


@pytest.mark.parametrize(
    ("ahora", "vigente"),
    [
        (VIGENTE_HASTA - MICRO, True),
        (VIGENTE_HASTA, False),
        (VIGENTE_HASTA + MICRO, False),
    ],
)
def test_vigencia_semiabierta(ahora: datetime, vigente: bool) -> None:
    """El instante exacto vigenteHasta ya es vencido."""
    assert oferta_vigente(VIGENTE_HASTA, ahora) is vigente


def test_vigencia_con_otra_zona_horaria() -> None:
    bogota = VIGENTE_HASTA.astimezone(datetime(2026, 1, 1).astimezone().tzinfo)
    assert oferta_vigente(VIGENTE_HASTA, bogota) is False


def test_forma_canonica_ida_y_vuelta(id_caso: str, sinteticos: Any) -> None:
    """Lo que se guarda es exactamente lo que entregó cotizacion (mismos campos y textos)."""
    original = sinteticos.caso(id_caso)["insumos"]
    assert insumos_canonicos(insumos_de(original)) == original


def test_huella_estable_y_sensible(sinteticos: Any) -> None:
    base = sinteticos.insumos("S01")
    assert hash_insumos(base) == hash_insumos(sinteticos.insumos("S01"))
    assert hash_insumos(base).startswith("sha256:")
    otra = dataclasses.replace(base, suma_asegurada=Decimal(180_000_001))
    assert hash_insumos(otra) != hash_insumos(base)


def test_los_insumos_no_tienen_fecha_de_nacimiento(sinteticos: Any) -> None:
    claves = set(insumos_canonicos(sinteticos.insumos("S01")))
    assert not {c for c in claves if "nacimiento" in c.lower()}
