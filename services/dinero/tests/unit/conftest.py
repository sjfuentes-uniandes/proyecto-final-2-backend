"""Datos de las pruebas unitarias de dinero (HU-W16). Solo datos sintéticos."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from dinero.domain.recaudo.modelos import Recaudo

AHORA = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
# Pagador sintético C01 del simulador de pagos (16.7): un clienteId de OfertaAceptada.
PAGADOR = UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def nuevo_pendiente(**cambios: Any) -> Recaudo:
    datos: dict[str, Any] = {
        "clave_recaudo": uuid4(),
        "cotizacion_id": uuid4(),
        "pagador_ref": PAGADOR,
        "monto": Decimal(64638),
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "correlation_id": "corr-w16",
        "ahora": AHORA,
    }
    datos.update(cambios)
    return Recaudo.pendiente(**datos)


@pytest.fixture
def pendiente() -> Callable[..., Recaudo]:
    return nuevo_pendiente
