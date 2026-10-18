"""El contrato publicado (contracts/openapi/adaptador-identidad.yaml) es el que sirve la app."""

from pathlib import Path

import yaml
from adaptador_identidad.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "adaptador-identidad.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_verificacion_documental() -> None:
    operacion = app.openapi()["paths"]["/v1/verificaciones/documento"]["post"]
    assert set(operacion["responses"]) == {"200", "400", "422", "502", "503", "504"}
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["SolicitudVerificacionDocumento"]["required"]) == {
        "clienteId",
        "tipoDocumento",
        "numeroDocumento",
        "nombreCompleto",
        "fechaNacimiento",
    }
    # Sin biometría: ni la solicitud ni la respuesta la contemplan.
    assert "selfie" not in esquemas["SolicitudVerificacionDocumento"]["properties"]
    assert esquemas["ResultadoDocumento"]["enum"] == ["APROBADO", "RECHAZADO"]
    assert set(esquemas["VerificacionDocumentoDto"]["required"]) == {
        "resultado",
        "motivo",
        "referencia",
        "proveedor",
    }


def test_contrato_de_verificaciones() -> None:
    operacion = app.openapi()["paths"]["/v1/verificaciones"]["post"]
    assert set(operacion["responses"]) == {"200", "400", "422", "502", "503", "504"}
    assert [p["name"] for p in operacion["parameters"]] == ["X-Deadline-Ms"]
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["SolicitudVerificacion"]["required"]) == {
        "clienteId",
        "selfie",
        "pruebaDeVida",
    }
    assert esquemas["ResultadoVerificacion"]["enum"] == [
        "APROBADO",
        "CAPTURA_INVALIDA",
        "RECHAZADO",
    ]
