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


def test_contrato_de_consentimientos() -> None:
    """HU-M08: rutas sin `clienteId` (el titular sale del token) y lista como arreglo simple."""
    rutas = app.openapi()["paths"]
    otorgar = rutas["/v1/consentimientos"]["post"]
    assert set(otorgar["responses"]) == {
        "200",
        "201",
        "400",
        "401",
        "403",
        "404",
        "409",
        "422",
        "503",
    }
    assert "Idempotent-Replayed" in otorgar["responses"]["200"]["headers"]
    clave = next(p for p in otorgar["parameters"] if p["name"] == "Idempotency-Key")
    assert clave["required"] is True and clave["schema"]["format"] == "uuid"
    listar = rutas["/v1/consentimientos"]["get"]
    assert listar["responses"]["200"]["content"]["application/json"]["schema"]["type"] == "array"
    assert set(rutas["/v1/consentimientos/{consentimientoId}"]["get"]["responses"]) == {
        "200",
        "401",
        "403",
        "404",
        "422",
        "503",
    }
    assert "/v1/consentimientos/texto-vigente" in rutas
    for ruta, operaciones in rutas.items():
        if ruta.startswith("/v1/consentimientos"):
            for operacion in operaciones.values():
                assert operacion["security"] == [{"bearerAuth": []}]
                assert all("cliente" not in p["name"].lower() for p in operacion["parameters"])
    esquemas = app.openapi()["components"]["schemas"]
    assert esquemas["EstadoConsentimiento"]["enum"] == ["ACTIVO", "REVOCADO", "VENCIDO"]
    assert "required" not in esquemas["SolicitudOtorgar"]
