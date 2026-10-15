"""Dominio puro: servicios autorizados según el estado de identidad."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID

import pytest
from bff_movil.domain.sesion.modelos import (
    SERVICIOS_POR_ESTADO,
    EstadoIdentidad,
    ServicioAutorizado,
    Sesion,
    servicios_autorizados,
)


@pytest.mark.hu("M07", "AC1")
def test_identidad_verificada_autoriza_polizas_siniestros_y_privacidad() -> None:
    assert servicios_autorizados(EstadoIdentidad.VERIFICADO) == (
        ServicioAutorizado.POLIZAS,
        ServicioAutorizado.SINIESTROS,
        ServicioAutorizado.PRIVACIDAD,
    )


@pytest.mark.parametrize("estado", [EstadoIdentidad.PENDIENTE, EstadoIdentidad.RECHAZADO])
def test_identidad_no_verificada_solo_autoriza_verificar_la_identidad(
    estado: EstadoIdentidad,
) -> None:
    assert servicios_autorizados(estado) == (ServicioAutorizado.VERIFICACION_IDENTIDAD,)


def test_todo_estado_de_identidad_tiene_servicios_definidos() -> None:
    assert set(SERVICIOS_POR_ESTADO) == set(EstadoIdentidad)


def test_la_sesion_es_inmutable() -> None:
    sesion = Sesion(
        cliente_id=UUID("00000000-0000-4000-8000-000000000001"),
        servicios_autorizados=(),
        expira_en=datetime(2026, 10, 14, tzinfo=UTC),
    )

    with pytest.raises(FrozenInstanceError):
        sesion.expira_en = datetime(2027, 1, 1, tzinfo=UTC)  # type: ignore[misc]
