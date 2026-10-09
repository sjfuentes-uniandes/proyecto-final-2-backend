"""El contrato publicado para el frontend (contracts/openapi/bff-web.yaml) es el que sirve la app.

Si cambia la API, regenerar con:
    uv run python -c "import yaml; from bff_web.main import app; \
open('contracts/openapi/bff-web.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
y sincronizar el frontend con scripts/sync-contracts.sh <ref>.
"""

from pathlib import Path

import yaml
from bff_web.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "bff-web.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_trazas() -> None:
    operacion = app.openapi()["paths"]["/operacion/trazas"]["get"]
    assert {p["name"] for p in operacion["parameters"] if p["in"] == "query"} == {
        "correlation_id", "recorrido", "duracion_min_ms", "horas",
    }
    assert set(operacion["responses"]) >= {"200", "422"}
