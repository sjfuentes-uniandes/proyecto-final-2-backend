import json
from importlib.resources import files
from typing import Any

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def definicion_candidata() -> dict[str, Any]:
    """Modelo candidato 2026.10.1 de 04 §B.6.1 (una copia nueva por prueba)."""
    semilla = files("catalogo.adapters.outbound") / "semillas" / "modelo_2026_10_1.json"
    return json.loads(semilla.read_text(encoding="utf-8"))
