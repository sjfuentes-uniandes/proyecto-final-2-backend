"""El contrato publicado (contracts/openapi/adaptador-pagos.yaml) es el que sirve la app.

El Pact `dinero` → `adaptador-pagos` (solo 200 aprobado y rechazado) llega en la 16.10: su
verificación de proveedor irá en este directorio (`test_pact_dinero.py`), con la app real y el
proveedor de pagos simulado.
"""

from pathlib import Path

import yaml
from adaptador_pagos.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "adaptador-pagos.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_del_cobro() -> None:
    operacion = app.openapi()["paths"]["/v1/cobros"]["post"]
    assert set(operacion["responses"]) == {"200", "400", "422", "502", "503", "504"}
    assert "parameters" not in operacion
    esquemas = app.openapi()["components"]["schemas"]
    solicitud = esquemas["SolicitudCobro"]
    assert set(solicitud["required"]) == {"claveRecaudo", "monto", "moneda", "pagadorRef"}
    assert solicitud["properties"]["claveRecaudo"]["format"] == "uuid"
    assert solicitud["properties"]["pagadorRef"]["format"] == "uuid"
    # Texto decimal, nunca número: mayor que 0 y con 2 decimales como máximo.
    assert solicitud["properties"]["monto"]["type"] == "string"
    assert solicitud["properties"]["monto"]["pattern"] == r"^[0-9]+(\.[0-9]{1,2})?$"
    assert solicitud["properties"]["moneda"]["const"] == "COP"


def test_contrato_de_la_consulta() -> None:
    operacion = app.openapi()["paths"]["/v1/cobros/{claveRecaudo}"]["get"]
    assert set(operacion["responses"]) == {"200", "404", "422", "502", "503", "504"}
    parametros = {p["name"]: p for p in operacion["parameters"]}
    assert set(parametros) == {"claveRecaudo", "pagadorRef"}
    assert parametros["claveRecaudo"]["in"] == "path"
    assert parametros["pagadorRef"]["in"] == "query"
    assert all(p["required"] for p in parametros.values())
    assert all(p["schema"]["format"] == "uuid" for p in parametros.values())


def test_la_respuesta_es_la_misma_en_las_dos_rutas() -> None:
    rutas = app.openapi()["paths"]
    for operacion in (rutas["/v1/cobros"]["post"], rutas["/v1/cobros/{claveRecaudo}"]["get"]):
        esquema = operacion["responses"]["200"]["content"]["application/json"]["schema"]
        assert esquema == {"$ref": "#/components/schemas/CobroDto"}
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["CobroDto"]["required"]) == {
        "claveRecaudo",
        "estado",
        "transaccionId",
        "motivo",
    }
    assert esquemas["EstadoCobro"]["enum"] == ["APROBADO", "RECHAZADO"]
