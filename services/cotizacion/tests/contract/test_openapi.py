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
    recalculo = rutas["/v1/perfiles/{perfilId}/recalculos"]["post"]
    assert set(recalculo["responses"]) == {"200", "201", "401", "403", "404", "409", "422", "503"}
    assert "requestBody" not in recalculo
    encabezados = {p["name"]: p["required"] for p in recalculo["parameters"] if p["in"] == "header"}
    assert encabezados == {"X-Actor-Id": False, "X-Actor-Rol": False, "Idempotency-Key": True}
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


# Obligatorios de PerfilRespuesta antes de HU-W31: los campos nuevos no pueden entrar aquí.
OBLIGATORIOS_V1 = [
    "id",
    "clienteId",
    "productoId",
    "estado",
    "nivelRiesgo",
    "puntaje",
    "confianza",
    "factores",
    "ajustes",
    "datosRequeridos",
    "accionSugerida",
    "consentimiento",
    "fuentes",
    "fuentesConsultadas",
    "fuentesDeclaradas",
    "versionModelo",
    "calculadoEn",
    "duracionMs",
    "presupuestoMs",
    "perfilAnteriorId",
]


def test_hu_w31_la_marca_de_revocacion_es_aditiva_y_opcional() -> None:
    esquemas = app.openapi()["components"]["schemas"]
    perfil = esquemas["PerfilRespuesta"]
    assert perfil["required"] == OBLIGATORIOS_V1
    comprobacion = perfil["properties"]["comprobacionFinal"]
    assert comprobacion["anyOf"] == [
        {"$ref": "#/components/schemas/ConsentimientoDto"},
        {"type": "null"},
    ]
    marca = perfil["properties"]["revocadoDurantePerfilamiento"]
    assert (marca["type"], marca["default"]) == ("boolean", False)
    # Sin valores de enumeración nuevos: la comprobación reutiliza DecisionConsentimiento.
    assert esquemas["DecisionConsentimiento"]["enum"] == [
        "PERMITIDO",
        "PERMITIDO_PARCIAL",
        "RECHAZADO",
    ]
    # ResumenPerfil (listados) no cambia en esta PR.
    assert not {"comprobacionFinal", "revocadoDurantePerfilamiento"} & set(
        esquemas["ResumenPerfil"]["properties"]
    )
