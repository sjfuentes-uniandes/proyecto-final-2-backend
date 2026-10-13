"""El contrato publicado (contracts/openapi/adaptador-datos.yaml) es el que sirve la app."""

from pathlib import Path

import yaml
from adaptador_datos.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "adaptador-datos.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_consultas() -> None:
    operacion = app.openapi()["paths"]["/v1/consultas"]["post"]
    assert set(operacion["responses"]) == {"200", "400", "422", "502", "503", "504"}
    assert [p["name"] for p in operacion["parameters"]] == ["X-Deadline-Ms"]
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["SolicitudConsulta"]["required"]) == {
        "fuente",
        "clienteId",
        "proposito",
        "camposAutorizados",
    }
    assert set(esquemas["RespuestaConsultaDto"]["properties"]) == {
        "fuente",
        "tipo",
        "proveedor",
        "versionFuente",
        "confianza",
        "consultadaEn",
        "campos",
        "camposDescartados",
    }
