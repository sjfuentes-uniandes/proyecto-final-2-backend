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


def test_contrato_de_alertas() -> None:
    operacion = app.openapi()["paths"]["/operacion/alertas"]["get"]
    esquemas = app.openapi()["components"]["schemas"]
    assert set(operacion["responses"]) >= {"200"}
    assert set(esquemas["AlertasRespuesta"]["properties"]) == {
        "ambiente", "porcentaje_advertencia", "falso_positivo_min", "atencion_min", "abiertas", "umbrales", "resumen",
    }
    assert esquemas["AlertaRespuesta"]["properties"]["severidad"]["enum"] == ["critica", "advertencia", "informativa"]
    assert esquemas["EnlaceRespuesta"]["properties"]["tipo"]["enum"] == ["traza", "socio", "cola", "tablero"]


COTIZACION = CONTRATO.parent / "cotizacion.yaml"
# HU-W11: el BFF publica los mismos esquemas que cotizacion para /v1/perfiles (04 W11-P15).
ESQUEMAS_PERFILAMIENTO = (
    "SolicitudPerfil", "DatosDeclaradosDto", "PerfilRespuesta", "ResumenPerfil", "ListaPerfiles",
    "FactorDto", "AjusteDto", "DatoRequeridoDto", "ConsentimientoDto", "FuenteDto",
    "AccionSugerida", "DecisionConsentimiento", "Direccion", "EstadoFactor", "EstadoFuente",
    "EstadoPerfil", "Fuente", "MotivoDato", "NivelRiesgo", "TipoFuente",
)


def test_dto_de_perfilamiento_iguales_a_cotizacion() -> None:
    propios = app.openapi()["components"]["schemas"]
    de_cotizacion = yaml.safe_load(COTIZACION.read_text(encoding="utf-8"))["components"]["schemas"]
    for nombre in ESQUEMAS_PERFILAMIENTO:
        assert propios[nombre] == de_cotizacion[nombre], nombre


def test_contrato_de_perfiles() -> None:
    rutas = app.openapi()["paths"]
    crear = rutas["/v1/perfiles"]["post"]
    assert set(crear["responses"]) == {"200", "201", "401", "403", "404", "409", "422", "503"}
    requeridos = {p["name"] for p in crear["parameters"] if p["required"]}
    assert requeridos == {"Idempotency-Key"}
    assert set(rutas["/v1/perfiles/{perfilId}"]["get"]["responses"]) == {"200", "401", "403", "404", "422", "503"}
    consulta = {p["name"] for p in rutas["/v1/perfiles"]["get"]["parameters"] if p["in"] == "query"}
    assert consulta == {"clienteId", "limite"}
    # HU-W12: recálculo y comparación (05 §B.4.2).
    recalculo = rutas["/v1/perfiles/{perfilId}/recalculos"]["post"]
    assert set(recalculo["responses"]) == {"200", "201", "401", "403", "404", "409", "422", "503"}
    assert {p["name"] for p in recalculo["parameters"] if p["required"]} == {"perfilId", "Idempotency-Key"}
    assert "requestBody" not in recalculo
    comparacion = rutas["/v1/perfiles/{perfilId}/comparacion"]["get"]
    assert set(comparacion["responses"]) == {"200", "401", "403", "404", "422", "503"}
    assert app.openapi()["components"]["schemas"]["ComparacionPerfiles"]["required"] == ["anterior", "actual"]
