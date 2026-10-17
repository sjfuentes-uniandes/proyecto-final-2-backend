"""CT-W11-02: los esquemas publicados en contracts/events son los que genera el código.

Si cambia un evento, regenerar con: uv run python -m cotizacion.adapters.outbound.eventos
"""

import json
from pathlib import Path

import pytest
from cotizacion.adapters.outbound.eventos import ESQUEMAS
from pydantic import BaseModel

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"


@pytest.mark.parametrize(("archivo", "modelo"), ESQUEMAS.items())
def test_ct_w11_02_esquema_publicado_coincide(archivo: str, modelo: type[BaseModel]) -> None:
    publicado = json.loads((EVENTOS / archivo).read_text(encoding="utf-8"))
    assert publicado == modelo.model_json_schema(by_alias=True)


def test_ct_w11_02_los_esquemas_del_codigo_estan_publicados() -> None:
    """Otros servicios (clientes) publican sus propios esquemas en el mismo directorio."""
    assert set(ESQUEMAS) <= {p.name for p in EVENTOS.glob("*.json")}
