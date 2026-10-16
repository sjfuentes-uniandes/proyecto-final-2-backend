from datetime import UTC, datetime
from uuid import UUID

import pytest
from clientes.domain.identidad.modelos import Cliente, EstadoIdentidad, ResultadoKyc

AHORA = datetime(2026, 10, 15, 15, tzinfo=UTC)
NUEVO = Cliente(id=UUID("00000000-0000-4000-8000-000000000001"), sub="sub-1")


def test_cliente_nuevo_queda_pendiente() -> None:
    assert NUEVO.estado is EstadoIdentidad.PENDIENTE
    assert NUEVO.verificado_en is None
    assert not NUEVO.verificado


def test_aprobado_verifica_y_fecha_la_verificacion() -> None:
    cliente = NUEVO.con_resultado(ResultadoKyc.APROBADO, AHORA)
    assert cliente.estado is EstadoIdentidad.VERIFICADO
    assert cliente.verificado_en == AHORA
    assert cliente.verificado


def test_rechazado_rechaza_sin_fecha() -> None:
    cliente = NUEVO.con_resultado(ResultadoKyc.RECHAZADO, AHORA)
    assert cliente.estado is EstadoIdentidad.RECHAZADO
    assert cliente.verificado_en is None


def test_captura_invalida_no_rechaza_al_cliente() -> None:
    """AC2: una selfie mal tomada no deja al cliente rechazado de forma definitiva."""
    assert NUEVO.con_resultado(ResultadoKyc.CAPTURA_INVALIDA, AHORA) == NUEVO


@pytest.mark.parametrize("previo", [ResultadoKyc.RECHAZADO, ResultadoKyc.CAPTURA_INVALIDA])
def test_se_puede_verificar_despues_de_un_intento_fallido(previo: ResultadoKyc) -> None:
    cliente = NUEVO.con_resultado(previo, AHORA).con_resultado(ResultadoKyc.APROBADO, AHORA)
    assert cliente.verificado
