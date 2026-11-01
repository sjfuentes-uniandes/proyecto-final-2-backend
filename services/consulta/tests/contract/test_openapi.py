"""El contrato publicado (contracts/openapi/consulta.yaml) es el que consume `bff-movil`."""

from pathlib import Path

import yaml
from consulta.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "consulta.yaml"
RUTA = "/v1/clientes/{cliente_id}/polizas"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_lectura_de_polizas() -> None:
    rutas = app.openapi()["paths"]
    lista, detalle = rutas[RUTA]["get"], rutas[f"{RUTA}/{{poliza_id}}"]["get"]
    assert set(lista["responses"]) == {"200", "401", "403", "422"}
    assert set(detalle["responses"]) == {"200", "401", "403", "404", "422"}
    for operacion in (lista, detalle):
        nombres = {p["name"].lower() for p in operacion["parameters"]}
        assert {"x-actor-id", "x-actor-rol"} <= nombres
