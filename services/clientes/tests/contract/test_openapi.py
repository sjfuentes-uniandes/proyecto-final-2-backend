"""El contrato publicado (contracts/openapi/clientes.yaml) es el que sirve la app."""

from pathlib import Path

import yaml
from clientes.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "clientes.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_verificaciones() -> None:
    rutas = app.openapi()["paths"]
    verificar = rutas["/v1/clientes/actual/identidad/verificaciones"]["post"]
    assert set(verificar["responses"]) == {"200", "400", "401", "403", "422", "503"}
    assert set(rutas) == {
        "/v1/clientes/actual",
        "/v1/clientes/{cliente_id}/identidad",
        "/v1/clientes/actual/identidad/verificaciones",
        "/v1/registros/{registro_id}/verificaciones",
        "/v1/registros/{registro_id}/vinculacion",
        "/health",
    }
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["SolicitudVerificacion"]["required"]) == {"selfie", "pruebaDeVida"}
    assert set(esquemas["VerificacionDto"]["required"]) == {
        "clienteId",
        "estado",
        "resultado",
        "motivo",
        "verificadoEn",
    }
    assert esquemas["EstadoIdentidad"]["enum"] == ["VERIFICADO", "PENDIENTE", "RECHAZADO"]


def test_contrato_del_registro() -> None:
    rutas = app.openapi()["paths"]
    verificar = rutas["/v1/registros/{registro_id}/verificaciones"]["post"]
    vincular = rutas["/v1/registros/{registro_id}/vinculacion"]["post"]
    assert set(verificar["responses"]) == {"200", "400", "409", "422", "503"}
    assert set(vincular["responses"]) == {"200", "400", "401", "403", "409", "422"}
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["SolicitudVerificacionRegistro"]["required"]) == {
        "correo",
        "selfie",
        "pruebaDeVida",
    }
    assert set(esquemas["SolicitudVinculacion"]["required"]) == {"tokenRegistro"}
    # El token solo viaja con una verificación exitosa: es nulo en los demás casos.
    token = esquemas["VerificacionRegistroDto"]["properties"]["tokenRegistro"]
    assert {"type": "null"} in token["anyOf"]


def test_identidad_es_compatible_con_la_propuesta_de_cotizacion() -> None:
    """cotizacion lee {clienteId, estado, verificadoEn} de GET .../identidad."""
    propuesta = yaml.safe_load(
        (CONTRATO.parent / "clientes.propuesta-cotizacion.yaml").read_text(encoding="utf-8")
    )["components"]["schemas"]["Identidad"]
    publicado = app.openapi()["components"]["schemas"]["IdentidadDto"]
    assert set(publicado["required"]) == set(propuesta["required"])
    assert set(publicado["properties"]) == set(propuesta["properties"])
