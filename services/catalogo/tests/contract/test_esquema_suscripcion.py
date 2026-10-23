"""El esquema publicado de las reglas de suscripción
(contracts/modelos/reglas-suscripcion.v1.schema.json) es el que genera el código, y la respuesta de
la API es la que publica el OpenAPI de catalogo (HU-W13)."""

import json
from pathlib import Path

import pytest
from catalogo.adapters.inbound.http.esquema_suscripcion import DefinicionReglasSuscripcion, a_dict
from catalogo.adapters.outbound.reglas_suscripcion_pg import semillas
from catalogo.main import app
from fastapi.testclient import TestClient
from pydantic import ValidationError

ESQUEMA = Path(__file__).parents[4] / "contracts" / "modelos" / "reglas-suscripcion.v1.schema.json"


def test_esquema_publicado_coincide_con_el_generado() -> None:
    publicado = json.loads(ESQUEMA.read_text(encoding="utf-8"))
    assert publicado == DefinicionReglasSuscripcion.model_json_schema()


def test_las_semillas_cumplen_el_esquema() -> None:
    for _, _, _, definicion in semillas():
        assert a_dict(DefinicionReglasSuscripcion.model_validate(definicion)) == definicion


@pytest.mark.parametrize(
    "cambio",
    [
        {"extra": 1},
        {"reglas": []},
        {"productoId": "Mayusculas"},
    ],
)
def test_definiciones_fuera_del_esquema(cambio: dict[str, object]) -> None:
    definicion = dict(semillas()[0][3], **cambio)
    with pytest.raises(ValidationError):
        DefinicionReglasSuscripcion.model_validate(definicion)


def test_una_regla_fuera_del_esquema() -> None:
    definicion = semillas()[0][3]
    for cambio in ({"codigo": "SUS-1"}, {"campo": "ingreso"}, {"orden": 0}, {"valor": []}):
        regla = dict(definicion["reglas"][0], **cambio)
        with pytest.raises(ValidationError):
            DefinicionReglasSuscripcion.model_validate(dict(definicion, reglas=[regla]))


def test_la_respuesta_es_la_del_openapi(api: TestClient) -> None:
    respuesta = api.get("/v1/productos/vida-hipotecario/reglas-suscripcion/vigente").json()
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["ReglasSuscripcionRespuesta"]["properties"]) == set(respuesta)
    assert set(esquemas["ReglaSuscripcion"]["properties"]) == set(
        respuesta["definicion"]["reglas"][0]
    )
