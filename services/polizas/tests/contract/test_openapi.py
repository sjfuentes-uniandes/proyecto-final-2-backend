"""CT-W13-01: el contrato publicado (contracts/openapi/polizas.yaml) es el que sirve la app.

Si cambia la API, regenerar con:
    uv run python -c "import yaml; from polizas.main import app; \
open('contracts/openapi/polizas.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
y copiar los DTO cambiados en bff-web.
"""

from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from fastapi.testclient import TestClient
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


def test_contrato_de_la_comunicacion() -> None:
    """HU-W14: `comunicacion` es opcional (un cliente de W13 sigue validando) y cerrada."""
    esquemas = app.openapi()["components"]["schemas"]
    decision = esquemas["DecisionSuscripcion"]
    assert "comunicacion" in decision["properties"]
    assert "comunicacion" not in decision["required"]
    comunicacion = esquemas["ComunicacionDto"]
    assert (
        set(comunicacion["required"])
        == set(comunicacion["properties"])
        == {
            "condicion",
            "admiteContratacion",
            "siguientePaso",
            "vigenteHasta",
            "motivoCliente",
            "alternativas",
            "plazoRespuestaHoras",
        }
    )
    assert esquemas["CondicionAseguramiento"]["enum"] == [
        "APROBADA_SIN_AJUSTES",
        "NO_ASEGURABLE",
        "EN_REVISION",
    ]
    assert esquemas["SiguientePaso"]["enum"] == [
        "ACEPTAR_OFERTA",
        "COTIZAR_NUEVAMENTE",
        "COTIZAR_ALTERNATIVA",
        "ESPERAR_REVISION",
    ]
    assert esquemas["CodigoDescriptor"]["enum"] == ["EDAD_MAXIMA", "SIN_LIMITE_EDAD"]


def test_una_decision_de_w13_sin_comunicacion_sigue_siendo_valida(
    api: TestClient, sinteticos: Any
) -> None:
    """Criterio técnico 5: un cuerpo de W13 (sin `comunicacion`) valida contra el contrato nuevo."""
    from polizas.adapters.inbound.http.suscripciones import DecisionSuscripcion

    respuesta = api.post(
        "/v1/suscripciones",
        json={"cotizacionId": sinteticos.cotizacion_id("S01")},
        headers={"X-Actor-Id": "a", "X-Actor-Rol": "ASESOR", "Idempotency-Key": str(uuid4())},
    )
    de_w13 = {k: v for k, v in respuesta.json().items() if k != "comunicacion"}
    assert DecisionSuscripcion.model_validate(de_w13).comunicacion is None
