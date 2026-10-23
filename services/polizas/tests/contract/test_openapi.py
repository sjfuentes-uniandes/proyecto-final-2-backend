"""CT-W13-01: el contrato publicado (contracts/openapi/polizas.yaml) es el que sirve la app.

Si cambia la API, regenerar con:
    uv run python -c "import yaml; from polizas.main import app; \
open('contracts/openapi/polizas.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
y copiar los DTO cambiados en bff-web.
"""

from pathlib import Path

import yaml
from polizas.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "polizas.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_suscripciones() -> None:
    rutas = app.openapi()["paths"]
    solicitar = rutas["/v1/suscripciones"]["post"]
    assert set(solicitar["responses"]) == {"200", "201", "401", "403", "404", "409", "422", "503"}
    cabeceras = {p["name"]: p["required"] for p in solicitar["parameters"] if p["in"] == "header"}
    assert cabeceras == {"Idempotency-Key": True, "X-Actor-Id": False, "X-Actor-Rol": False}
    assert set(rutas) == {
        "/v1/suscripciones",
        "/v1/suscripciones/bandeja",
        "/v1/suscripciones/resumen",
        "/v1/suscripciones/{decisionId}",
        "/v1/suscripciones/{decisionId}/reconstruccion",
        "/health",
    }
    esquemas = app.openapi()["components"]["schemas"]
    assert esquemas["ResultadoDecision"]["enum"] == ["APROBADA", "RECHAZADA", "REVISION_REQUERIDA"]
    assert esquemas["ResultadoRegla"]["enum"] == ["CUMPLE", "NO_CUMPLE", "NO_EVALUABLE"]
    assert {"reservada", "valorCaso", "visibilidad"} <= set(
        esquemas["ReglaEvaluadaDto"]["properties"]
    )
