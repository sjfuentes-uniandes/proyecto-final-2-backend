"""El contrato publicado (contracts/openapi/avisos.yaml) es el que consume `bff-movil`."""

from pathlib import Path

import yaml
from avisos.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "avisos.yaml"
RUTA = "/v1/clientes/{cliente_id}/dispositivos"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_dispositivos() -> None:
    rutas = app.openapi()["paths"]
    registro, baja = rutas[RUTA]["post"], rutas[f"{RUTA}/{{dispositivo_id}}"]["delete"]
    assert set(registro["responses"]) == {"201", "401", "403", "422", "503"}
    assert set(baja["responses"]) == {"204", "401", "403", "404", "422", "503"}
    for operacion in (registro, baja):
        nombres = {p["name"].lower() for p in operacion["parameters"]}
        assert {"x-actor-id", "x-actor-rol"} <= nombres


def test_el_token_solo_viaja_en_el_cuerpo_y_no_se_devuelve() -> None:
    rutas = app.openapi()["paths"]
    esquemas = app.openapi()["components"]["schemas"]
    for ruta, operaciones in rutas.items():
        assert "token" not in ruta.lower().replace("dispositivos", "")
        for operacion in operaciones.values():
            assert all("token" not in p["name"].lower() for p in operacion.get("parameters", []))
    assert set(esquemas["SolicitudRegistro"]["required"]) == {"token"}
    assert "token" not in esquemas["DispositivoDto"]["properties"]
    assert set(esquemas["DispositivoDto"]["required"]) == {"dispositivoId"}
