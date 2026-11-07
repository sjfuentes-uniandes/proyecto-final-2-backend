"""El esquema publicado de `DescarteParametricoResuelto.v1` es el que genera el modelo (HU-W24).

Regenerar con `uv run python -m pagos_parametricos.adapters.outbound.eventos` (desde la raíz).
"""

import json
from pathlib import Path

from pagos_parametricos.adapters.outbound.eventos import ESQUEMAS

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"


def test_esquemas_publicados_coinciden_con_los_modelos() -> None:
    for nombre, modelo in ESQUEMAS.items():
        publicado = json.loads((EVENTOS / nombre).read_text(encoding="utf-8"))
        assert publicado == modelo.model_json_schema(by_alias=True), nombre
