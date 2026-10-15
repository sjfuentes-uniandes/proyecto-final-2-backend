"""El contrato publicado (contracts/openapi/bff-movil.yaml) es el que sirve la app."""

from pathlib import Path

import yaml
from bff_movil.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "bff-movil.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_sesion() -> None:
    operacion = app.openapi()["paths"]["/v1/sesion"]["get"]
    assert set(operacion["responses"]) == {"200", "401", "403", "404", "422", "503"}
    assert [p["name"] for p in operacion["parameters"]] == [
        "x-authenticated-sub",
        "x-authenticated-issuer",
    ]
    assert operacion["security"] == [{"bearerAuth": []}]
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["SesionDto"]["required"]) == {
        "clienteId",
        "serviciosAutorizados",
        "expiraEn",
    }
    assert esquemas["ServicioAutorizado"]["enum"] == [
        "POLIZAS",
        "SINIESTROS",
        "PRIVACIDAD",
        "VERIFICACION_IDENTIDAD",
    ]
