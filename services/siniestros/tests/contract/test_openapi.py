"""El contrato publicado (contracts/openapi/siniestros.yaml) es el que sirve la app (HU-W19).

Si cambia la API, regenerar con:
    uv run python -c "import yaml; from siniestros.main import app; \
open('contracts/openapi/siniestros.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
y propagarlo a `bff-web` (contracts/openapi/bff-web.yaml).
"""

from pathlib import Path

import yaml
from siniestros.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "siniestros.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_las_rutas_son_las_de_la_historia() -> None:
    rutas = {
        (metodo.upper(), ruta)
        for ruta, operaciones in app.openapi()["paths"].items()
        for metodo in operaciones
    }
    assert rutas == {
        ("GET", "/v1/siniestros"),
        ("GET", "/v1/siniestros/resumen"),
        ("POST", "/v1/siniestros/siguiente"),
        ("POST", "/v1/siniestros/accesos-denegados"),
        ("GET", "/v1/siniestros/{siniestroId}"),
        ("POST", "/v1/siniestros/{siniestroId}/evidencias/{evidenciaId}/enlace"),
        ("GET", "/health"),
    }


def test_el_detalle_nunca_expone_la_clave_del_objeto_ni_una_url() -> None:
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["EvidenciaVista"]["properties"]) == {
        "id",
        "nombreArchivo",
        "tipo",
        "capturadaEn",
        "ubicacion",
        "integridad",
    }
    for nombre, esquema in esquemas.items():
        if nombre != "EnlaceEvidencia":
            assert not {"url", "claveObjeto", "clave"} & set(esquema.get("properties", {})), nombre


def test_el_enlace_solo_lo_emite_la_ruta_de_enlace() -> None:
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["EnlaceEvidencia"]["properties"]) == {"url", "expiraEn", "tipoContenido"}


def test_las_rutas_exigen_el_actor_y_la_siguiente_la_clave_de_idempotencia() -> None:
    rutas = app.openapi()["paths"]
    for ruta, operaciones in rutas.items():
        if ruta == "/health":
            continue
        for operacion in operaciones.values():
            cabeceras = {p["name"] for p in operacion["parameters"] if p["in"] == "header"}
            assert {"X-Actor-Id", "X-Actor-Rol", "X-Actor-Nombre"} <= cabeceras
    siguiente = rutas["/v1/siniestros/siguiente"]["post"]["parameters"]
    clave = next(p for p in siguiente if p["name"] == "Idempotency-Key")
    assert clave["required"] is True
