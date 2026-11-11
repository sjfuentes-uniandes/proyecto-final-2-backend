"""El contrato publicado (contracts/openapi/dinero.yaml) es el que sirve la app (HU-W23, W23-B02).

Es la primera ruta HTTP de `dinero`: `GET /v1/indemnizaciones?siniestroId=`. La app real la sirve el
router de `adapters/inbound/http/indemnizaciones.py`; el YAML sale de `app.openapi()`.
"""

from pathlib import Path

import yaml
from dinero.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "dinero.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_la_lectura() -> None:
    operacion = app.openapi()["paths"]["/v1/indemnizaciones"]["get"]
    assert set(operacion["responses"]) == {"200", "401", "403", "422", "503"}
    parametros = {p["name"]: p for p in operacion["parameters"]}
    assert set(parametros) == {"siniestroId", "X-Actor-Id", "X-Actor-Rol"}
    assert parametros["siniestroId"]["in"] == "query" and parametros["siniestroId"]["required"]
    assert parametros["siniestroId"]["schema"]["format"] == "uuid"
    assert not parametros["X-Actor-Id"]["required"] and not parametros["X-Actor-Rol"]["required"]
    esquema = operacion["responses"]["200"]["content"]["application/json"]["schema"]
    assert esquema == {"$ref": "#/components/schemas/ListaIndemnizacionesDto"}


def test_contrato_de_la_indemnizacion() -> None:
    esquemas = app.openapi()["components"]["schemas"]
    campos = {
        "id",
        "siniestroId",
        "tipo",
        "referencia",
        "monto",
        "moneda",
        "estado",
        "transaccionId",
        "motivo",
        "intentos",
        "duplicadosDescartados",
        "creadaEn",
        "confirmadaEn",
        "ultimoIntentoEn",
    }
    indemnizacion = esquemas["IndemnizacionDto"]
    assert set(indemnizacion["required"]) == campos == set(indemnizacion["properties"])
    assert indemnizacion["properties"]["tipo"]["const"] == "INDEMNIZACION"
    # Texto decimal, nunca número.
    assert indemnizacion["properties"]["monto"]["type"] == "string"
    assert esquemas["EstadoIndemnizacion"]["enum"] == ["PENDIENTE", "CONFIRMADA", "RECHAZADA"]
    assert esquemas["ListaIndemnizacionesDto"]["required"] == ["items"]


def test_el_contrato_no_expone_la_cuenta_ni_el_beneficiario() -> None:
    texto = CONTRATO.read_text(encoding="utf-8").lower()
    for prohibido in ("beneficiario", "cuenta", "tarjeta", "iban"):
        assert prohibido not in texto
