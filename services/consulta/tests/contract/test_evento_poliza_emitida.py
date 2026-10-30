"""El evento sintético con el que se prueba la proyección cumple PolizaEmitida.v1 (HU-M10)."""

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ESQUEMA = Path(__file__).parents[4] / "contracts" / "events" / "PolizaEmitida.v1.schema.json"


def _errores(sobre: dict[str, Any]) -> list[str]:
    esquema = json.loads(ESQUEMA.read_text(encoding="utf-8"))
    validador = Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    return [e.message for e in validador.iter_errors(sobre)]


def test_el_evento_sintetico_cumple_el_esquema_publicado(sobre_poliza: Any) -> None:
    assert _errores(sobre_poliza()) == []


def test_un_evento_sin_cliente_incumple_el_esquema(sobre_poliza: Any) -> None:
    sobre = sobre_poliza()
    del sobre["payload"]["clienteId"]
    assert _errores(sobre) != []
