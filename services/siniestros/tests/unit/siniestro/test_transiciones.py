"""Cambios de estado y código del siniestro (DS3-02, DS3-11)."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

import pytest
from siniestros.domain.siniestro.errores import SiniestroInvalido, TransicionInvalida
from siniestros.domain.siniestro.modelos import EstadoSiniestro, Siniestro
from siniestros.domain.siniestro.transiciones import asignar, codigo_siniestro

ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")


def test_tomar_un_reportado_lo_pasa_a_evaluacion(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = siniestro()
    asignado = asignar(caso, ANALISTA, ahora)
    assert asignado.estado is EstadoSiniestro.EN_EVALUACION
    assert asignado.analista_id == ANALISTA
    assert asignado.asignado_en == ahora
    assert asignado.version == caso.version + 1
    # El original no cambia (inmutable) y lo demás se conserva.
    assert caso.estado is EstadoSiniestro.REPORTADO
    assert asignado.codigo == caso.codigo
    assert asignado.reportado_en == caso.reportado_en


@pytest.mark.parametrize(
    "estado",
    [EstadoSiniestro.EN_EVALUACION, EstadoSiniestro.FALTA_SOPORTE, EstadoSiniestro.APROBADO],
)
def test_solo_se_asigna_un_reportado(
    siniestro: Callable[..., Siniestro], ahora: datetime, estado: EstadoSiniestro
) -> None:
    extra: dict[str, object] = {}
    if estado is not EstadoSiniestro.APROBADO:
        extra["analista_id"] = ANALISTA
    if estado is EstadoSiniestro.FALTA_SOPORTE:
        extra["pausado_desde"] = ahora
    if estado is EstadoSiniestro.APROBADO:
        extra["decidido_en"] = ahora
    caso = siniestro(estado=estado, **extra)
    with pytest.raises(TransicionInvalida) as error:
        asignar(caso, ANALISTA, ahora)
    assert error.value.desde is estado
    assert str(error.value) == f"{estado} -> {EstadoSiniestro.EN_EVALUACION}"


def test_asignar_exige_hora_con_zona(
    siniestro: Callable[..., Siniestro], ingenuo: datetime
) -> None:
    with pytest.raises(SiniestroInvalido, match="ahora"):
        asignar(siniestro(), ANALISTA, ingenuo)


@pytest.mark.parametrize(
    ("anio", "consecutivo", "esperado"),
    [
        (2026, 1000, "88-2026-1000"),
        (2026, 442, "88-2026-0442"),
        (2027, 1, "88-2027-0001"),
        (2026, 12345, "88-2026-12345"),
    ],
)
def test_el_codigo_es_88_anio_consecutivo(anio: int, consecutivo: int, esperado: str) -> None:
    assert codigo_siniestro(anio, consecutivo) == esperado


@pytest.mark.parametrize(("anio", "consecutivo"), [(1999, 1), (10000, 1), (2026, 0), (2026, -3)])
def test_el_codigo_rechaza_valores_fuera_de_rango(anio: int, consecutivo: int) -> None:
    with pytest.raises(SiniestroInvalido, match="codigo"):
        codigo_siniestro(anio, consecutivo)
