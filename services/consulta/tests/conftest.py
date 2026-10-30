"""Evento sintético `PolizaEmitida` v1, conforme a `contracts/events/PolizaEmitida.v1.schema.json`."""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

EVENTOS = Path(__file__).parents[3] / "contracts/events"
EMITIDA_EN = "2026-10-24T15:00:01.250000Z"

FabricaSobre = Callable[..., dict[str, Any]]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def sobre_poliza() -> FabricaSobre:
    """Fábrica de sobres; `**cambios` sobrescribe campos del payload y `sobre` los del sobre."""

    def fabricar(sobre: dict[str, Any] | None = None, **cambios: Any) -> dict[str, Any]:
        poliza_id = str(uuid4())
        payload: dict[str, Any] = {
            "polizaId": poliza_id,
            "numero": "100-2026-000001",
            "cotizacionId": str(uuid4()),
            "claveRecaudo": str(uuid4()),
            "clienteId": str(uuid4()),
            "producto": {"id": "vida-deudor", "version": "1.0.0"},
            "vigencia": {"desde": "2026-10-25", "hasta": "2027-10-24"},
            "prima": "64638.10",
            "sumaAsegurada": "50000000",
            "moneda": "COP",
            "periodicidad": "MENSUAL",
            "documento": {
                "clave": f"polizas/{poliza_id}.pdf",
                "versionId": "v1",
                "huella": "sha256:" + "a" * 64,
                "tamanoBytes": 2048,
            },
            "firma": {"firmaId": "FIRMA-0001", "selloTiempo": EMITIDA_EN},
            "emitidaEn": EMITIDA_EN,
        } | cambios
        return {
            "eventId": str(uuid4()),
            "aggregateId": payload["polizaId"],
            "type": "PolizaEmitida",
            "version": 1,
            "occurredAt": EMITIDA_EN,
            "correlationId": "corr-poliza-sintetica",
            "payload": payload,
        } | (sobre or {})

    return fabricar
