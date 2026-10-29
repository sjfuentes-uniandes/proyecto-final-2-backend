"""Datos de las pruebas unitarias de dinero (HU-W16). Solo datos sintéticos."""

import io
import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from dinero.domain.recaudo.modelos import Recaudo
from solventa_common.logging import FormateadorJson

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


class Logs:
    """Lo que se registró, como JSON (el formato de producción)."""

    def __init__(self, salida: io.StringIO) -> None:
        self.salida = salida

    def texto(self) -> str:
        return self.salida.getvalue()

    def registros(self) -> list[dict[str, Any]]:
        return [json.loads(linea) for linea in self.texto().splitlines() if linea.strip()]

    def de_nivel(self, nivel: str) -> list[dict[str, Any]]:
        return [r for r in self.registros() if r["level"] == nivel]


@pytest.fixture
def capturar_logs() -> Iterator[Callable[[], Logs]]:
    """Redirige el logger raíz a un buffer con el formateador JSON (patrón de adaptador-pagos).
    Se llama después de crear la app: instrumentar() reconfigura el logging."""
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level

    def capturar() -> Logs:
        salida = io.StringIO()
        handler = logging.StreamHandler(salida)
        handler.setFormatter(FormateadorJson("dinero", "pruebas"))
        raiz.handlers = [handler]
        raiz.setLevel(logging.INFO)
        return Logs(salida)

    yield capturar
    raiz.handlers, raiz.level = anteriores, nivel


@pytest.fixture
def logs(capturar_logs: Callable[[], Logs]) -> Logs:
    return capturar_logs()
