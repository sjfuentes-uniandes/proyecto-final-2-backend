"""El contrato publicado (contracts/openapi/cotizacion.yaml) es el que sirve la app.

Si cambia la API, regenerar con:
    uv run python -c "import yaml; from cotizacion.main import app; \
open('contracts/openapi/cotizacion.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
"""

from pathlib import Path

import yaml
from cotizacion.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "cotizacion.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_perfiles() -> None:
    rutas = app.openapi()["paths"]
    crear = rutas["/v1/perfiles"]["post"]
    assert set(crear["responses"]) == {"200", "201", "401", "403", "404", "409", "422", "503"}
    encabezados = {p["name"]: p["required"] for p in crear["parameters"] if p["in"] == "header"}
    assert encabezados == {"X-Actor-Id": False, "X-Actor-Rol": False, "Idempotency-Key": True}
    assert set(rutas["/v1/perfiles"]["get"]["responses"]) == {"200", "401", "403", "422"}
    for ruta in ("/v1/perfiles/{perfilId}", "/v1/perfiles/{perfilId}/reconstruccion"):
        assert set(rutas[ruta]["get"]["responses"]) == {"200", "401", "403", "404", "422"}
    esquemas = app.openapi()["components"]["schemas"]
    respuesta = set(esquemas["PerfilRespuesta"]["properties"])
    assert not {"insumos", "datosDeclarados", "hashModelo"} & respuesta
    assert set(esquemas["ResumenPerfil"]["properties"]) == {
        "id",
        "estado",
        "nivelRiesgo",
        "confianza",
        "versionModelo",
        "calculadoEn",
        "perfilAnteriorId",
    }
