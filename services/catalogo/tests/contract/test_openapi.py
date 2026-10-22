"""El contrato publicado (contracts/openapi/catalogo.yaml) es el que sirve la app.

Si cambia la API, regenerar con:
    uv run python -c "import yaml; from catalogo.main import app; \
open('contracts/openapi/catalogo.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
"""

from pathlib import Path

import yaml
from catalogo.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "catalogo.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_modelos_de_riesgo() -> None:
    rutas = app.openapi()["paths"]
    assert set(rutas["/v1/modelos-riesgo"]["post"]["responses"]) == {
        "201",
        "401",
        "403",
        "409",
        "422",
    }
    for ruta in ("/v1/modelos-riesgo/vigente", "/v1/modelos-riesgo/{version}"):
        assert set(rutas[ruta]["get"]["responses"]) == {"200", "404", "422"}
    parametros = rutas["/v1/modelos-riesgo/vigente"]["get"]["parameters"]
    assert {p["name"]: p["required"] for p in parametros} == {"productoId": True, "en": False}
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["ModeloRiesgoRespuesta"]["properties"]) == {
        "version",
        "productoId",
        "vigenteDesde",
        "hash",
        "definicion",
    }


def test_contrato_de_reglas_de_suscripcion() -> None:
    """HU-W13 (DS2-18): solo lectura, con el patrón de las reglas de prima."""
    rutas = app.openapi()["paths"]
    base = "/v1/productos/{productoId}/reglas-suscripcion"
    assert set(rutas[f"{base}/vigente"]) == {"get"}
    assert set(rutas[f"{base}/{{version}}"]) == {"get"}
    for ruta in (f"{base}/vigente", f"{base}/{{version}}"):
        assert set(rutas[ruta]["get"]["responses"]) == {"200", "404", "422"}
    parametros = rutas[f"{base}/vigente"]["get"]["parameters"]
    assert {p["name"]: p["required"] for p in parametros} == {"productoId": True, "fecha": False}
    esquemas = app.openapi()["components"]["schemas"]
    assert esquemas["CampoRegla"]["enum"] == [
        "edad",
        "suma_asegurada",
        "plazo_credito_meses",
        "nivel_riesgo",
        "confianza_perfil",
        "coincidencias_listas_restrictivas",
    ]
    assert set(esquemas["ReglasSuscripcionRespuesta"]["properties"]) == {
        "version",
        "productoId",
        "vigenteDesde",
        "hash",
        "definicion",
    }
