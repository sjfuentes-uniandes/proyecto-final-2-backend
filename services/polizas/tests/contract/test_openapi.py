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
        "/v1/cotizaciones/{cotizacionId}/aceptacion",
        "/v1/cotizaciones/{cotizacionId}/recaudo",
        "/v1/cotizaciones/{cotizacionId}/poliza",
        "/v1/polizas/{polizaId}/documento",
        "/v1/polizas/verificaciones",
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


def test_contrato_de_la_aceptacion() -> None:
    """HU-W15: POST con Idempotency-Key obligatoria y sus errores; respuesta cerrada."""
    ruta = app.openapi()["paths"]["/v1/cotizaciones/{cotizacionId}/aceptacion"]
    registrar, consultar = ruta["post"], ruta["get"]
    assert set(registrar["responses"]) == {"200", "201", "401", "403", "404", "409", "422", "503"}
    assert set(consultar["responses"]) == {"200", "401", "403", "404", "422"}
    cabeceras = {p["name"]: p["required"] for p in registrar["parameters"] if p["in"] == "header"}
    assert cabeceras == {"Idempotency-Key": True, "X-Actor-Id": False, "X-Actor-Rol": False}
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["SolicitudAceptacion"]["required"]) == {
        "decisionId",
        "versionDeclaraciones",
        "declaraciones",
    }
    assert set(esquemas["DeclaracionesDto"]["required"]) == {
        "leidoCondiciones",
        "informacionVeraz",
        "debitoAutomatico",
        "avisosElectronicos",
    }
    aceptacion = esquemas["Aceptacion"]
    assert (
        set(aceptacion["required"])
        == set(aceptacion["properties"])
        == {
            "id",
            "cotizacionId",
            "ofertaId",
            "decisionId",
            "clienteId",
            "huellaCalculo",
            "versionDeclaraciones",
            "declaraciones",
            "canal",
            "actorId",
            "aceptadaEn",
            "huellaEvidencia",
            "recaudoHabilitado",
            "siguientePaso",
        }
    )
    assert esquemas["CanalAceptacion"]["enum"] == ["PORTAL_ASESOR"]
    assert esquemas["SiguientePasoContratacion"]["enum"] == ["FIRMA"]


def test_contrato_del_recaudo() -> None:
    """HU-W16 (16.13): solo lectura, con los encabezados y errores de GET …/aceptacion; respuesta
    cerrada, con los opcionales como nulos y el monto como texto decimal."""
    rutas = app.openapi()["paths"]
    ruta = rutas["/v1/cotizaciones/{cotizacionId}/recaudo"]
    assert set(ruta) == {"get"}
    consultar = ruta["get"]
    aceptacion = rutas["/v1/cotizaciones/{cotizacionId}/aceptacion"]["get"]
    assert set(consultar["responses"]) == set(aceptacion["responses"])
    assert consultar["parameters"] == aceptacion["parameters"]
    assert consultar["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EstadoRecaudo"
    }
    esquemas = app.openapi()["components"]["schemas"]
    estado = esquemas["EstadoRecaudo"]
    assert (
        set(estado["required"])
        == set(estado["properties"])
        == {
            "estado",
            "claveRecaudo",
            "monto",
            "moneda",
            "transaccionId",
            "motivo",
            "actualizadoEn",
        }
    )
    assert estado["properties"]["monto"]["type"] == "string"
    assert estado["properties"]["monto"]["pattern"] == r"^-?\d+(\.\d+)?$"
    for nullable in ("transaccionId", "motivo"):
        tipos = {t.get("type") for t in estado["properties"][nullable]["anyOf"]}
        assert tipos == {"string", "null"}, nullable
    assert esquemas["SituacionRecaudo"]["enum"] == ["PENDIENTE", "CONFIRMADO", "RECHAZADO"]


def _nullable(propiedad: dict[str, Any]) -> set[Any]:
    return {t.get("type", t.get("$ref")) for t in propiedad["anyOf"]}


def test_contrato_de_la_poliza() -> None:
    """HU-W17 (17.9): la póliza por cotización, con los encabezados y errores de GET …/aceptacion;
    respuesta cerrada, con los puntos de control como nulos mientras se emite y sin lo interno."""
    rutas = app.openapi()["paths"]
    ruta = rutas["/v1/cotizaciones/{cotizacionId}/poliza"]
    assert set(ruta) == {"get"}
    aceptacion = rutas["/v1/cotizaciones/{cotizacionId}/aceptacion"]["get"]
    assert set(ruta["get"]["responses"]) == set(aceptacion["responses"])
    assert ruta["get"]["parameters"] == aceptacion["parameters"]
    assert ruta["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Poliza"
    }
    esquemas = app.openapi()["components"]["schemas"]
    poliza = esquemas["Poliza"]
    assert (
        set(poliza["required"])
        == set(poliza["properties"])
        == {
            "polizaId",
            "estado",
            "numero",
            "producto",
            "ramo",
            "coberturas",
            "vigencia",
            "sumaAsegurada",
            "prima",
            "moneda",
            "periodicidad",
            "aceptadaEn",
            "recaudadaEn",
            "emitidaEn",
            "documento",
            "firma",
        }
    )
    assert esquemas["EstadoPoliza"]["enum"] == ["EMITIENDO", "ACTIVA"]
    for monto in ("sumaAsegurada", "prima"):
        assert poliza["properties"][monto]["type"] == "string"
        assert poliza["properties"][monto]["pattern"] == r"^-?\d+(\.\d+)?$"
    assert poliza["properties"]["numero"]["pattern"] == r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$"
    assert _nullable(poliza["properties"]["emitidaEn"]) == {"string", "null"}
    assert _nullable(poliza["properties"]["documento"]) == {
        "#/components/schemas/DocumentoPolizaDto",
        "null",
    }
    assert _nullable(poliza["properties"]["firma"]) == {
        "#/components/schemas/FirmaPolizaDto",
        "null",
    }
    assert set(esquemas["DocumentoPolizaDto"]["properties"]) == {"huella", "versionId"}
    assert set(esquemas["FirmaPolizaDto"]["properties"]) == {"firmaId", "selloTiempo"}
    vigencia = esquemas["VigenciaPolizaDto"]["properties"]
    assert vigencia["desde"]["format"] == vigencia["hasta"]["format"] == "date"


def test_contrato_del_documento_y_la_verificacion() -> None:
    """HU-W17 (17.9): la URL del documento y la verificación, con los encabezados del actor."""
    rutas = app.openapi()["paths"]
    documento = rutas["/v1/polizas/{polizaId}/documento"]
    assert set(documento) == {"get"}
    assert set(documento["get"]["responses"]) == {"200", "401", "403", "404", "409", "422", "503"}
    cabeceras = {
        p["name"]: p["required"] for p in documento["get"]["parameters"] if p["in"] == "header"
    }
    assert cabeceras == {"X-Actor-Id": False, "X-Actor-Rol": False}
    verificaciones = rutas["/v1/polizas/verificaciones"]
    assert set(verificaciones) == {"post"}
    verificar = verificaciones["post"]
    assert set(verificar["responses"]) == {"200", "401", "403", "422"}
    cabeceras = {p["name"]: p["required"] for p in verificar["parameters"] if p["in"] == "header"}
    assert cabeceras == {"X-Actor-Id": False, "X-Actor-Rol": False}
    esquemas = app.openapi()["components"]["schemas"]
    enlace = esquemas["EnlaceDocumento"]
    assert set(enlace["required"]) == set(enlace["properties"]) == {"url", "venceEn"}
    solicitud = esquemas["SolicitudVerificacion"]
    assert set(solicitud["required"]) == set(solicitud["properties"]) == {"numero", "huella"}
    assert solicitud["properties"]["huella"]["pattern"] == r"^sha256:[0-9a-f]{64}$"
    assert solicitud["properties"]["numero"]["pattern"] == r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$"
    resultado = esquemas["VerificacionPoliza"]
    assert (
        set(resultado["required"])
        == set(resultado["properties"])
        == {"valido", "numero", "firmaId", "selloTiempo"}
    )
    for nullable in ("firmaId", "selloTiempo"):
        assert _nullable(resultado["properties"][nullable]) == {"string", "null"}, nullable


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
