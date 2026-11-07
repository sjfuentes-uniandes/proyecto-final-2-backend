"""El contrato publicado (contracts/openapi/pagos-parametricos.yaml) es el que consume `bff-web`.

Si cambia la API, regenerar con:
    uv run python -c "import yaml; from pagos_parametricos.main import app; \
open('contracts/openapi/pagos-parametricos.yaml', 'w').write(yaml.safe_dump(app.openapi(), \
sort_keys=False, allow_unicode=True))"
"""

from pathlib import Path

import yaml
from pagos_parametricos.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "pagos-parametricos.yaml"
RUTA = "/v1/parametricos/descartes"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_la_cola_de_descarte() -> None:
    rutas = app.openapi()["paths"]
    assert set(rutas[RUTA]["get"]["responses"]) == {"200", "401", "403", "422", "503"}
    assert set(rutas[f"{RUTA}/{{messageId}}"]["get"]["responses"]) == {
        "200",
        "401",
        "403",
        "404",
        "422",
        "503",
    }
    assert set(rutas[f"{RUTA}/{{messageId}}/descarte"]["post"]["responses"]) == {
        "200",
        "401",
        "403",
        "404",
        "409",
        "422",
        "503",
    }
