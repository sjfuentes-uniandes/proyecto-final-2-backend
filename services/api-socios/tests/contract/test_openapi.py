"""El contrato publicado (contracts/openapi/api-socios.yaml) es el que sirve la app.

Si cambia la API, regenerar con:
    uv run python -c "import yaml; from api_socios.main import app; \
open('contracts/openapi/api-socios.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
"""

from pathlib import Path

import yaml
from api_socios.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "api-socios.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_documenta_los_codigos_de_la_historia() -> None:
    rutas = app.openapi()["paths"]
    assert set(rutas["/admin/socios"]["post"]["responses"]) >= {"201", "400"}
    assert set(rutas["/socio/me"]["get"]["responses"]) >= {"200", "401"}
    assert set(rutas["/polizas"]["post"]["responses"]) >= {"401", "403"}
    assert "422" not in str(rutas)
    creado = app.openapi()["components"]["schemas"]["SocioCreado"]["properties"]
    respuesta = app.openapi()["components"]["schemas"]["SocioRespuesta"]["properties"]
    assert {"clientSecret", "apiKey"} <= set(creado) and not {"clientSecret", "apiKey"} & set(respuesta)
