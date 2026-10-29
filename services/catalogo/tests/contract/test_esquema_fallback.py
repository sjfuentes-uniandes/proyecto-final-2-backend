"""El esquema publicado de las reglas de fallback (contracts/modelos/reglas-fallback.v1.schema.json)
es el que genera el código, y la respuesta de la API es la que publica el OpenAPI (HU-W06)."""

import json
from pathlib import Path

import pytest
from catalogo.adapters.inbound.http.esquema_fallback import DefinicionReglasFallback, a_dict
from catalogo.adapters.outbound.modelos_pg import SEMILLA_PRODUCTO, definicion_semilla
from catalogo.adapters.outbound.reglas_fallback_pg import semillas
from catalogo.domain.fallback.errores import ReglasFallbackInvalidas
from catalogo.main import app
from fastapi.testclient import TestClient
from pydantic import ValidationError

ESQUEMA = Path(__file__).parents[4] / "contracts" / "modelos" / "reglas-fallback.v1.schema.json"
MODELOS = {SEMILLA_PRODUCTO: definicion_semilla()}


def test_esquema_publicado_coincide_con_el_generado() -> None:
    publicado = json.loads(ESQUEMA.read_text(encoding="utf-8"))
    assert publicado == DefinicionReglasFallback.model_json_schema()


def test_las_semillas_cumplen_el_esquema() -> None:
    for *_, definicion in semillas(MODELOS):
        assert a_dict(DefinicionReglasFallback.model_validate(definicion)) == definicion


def test_sin_modelo_de_riesgo_la_semilla_no_se_publica() -> None:
    with pytest.raises(ReglasFallbackInvalidas) as error:
        semillas({})
    assert error.value.errores == ("SIN_MODELO_DE_RIESGO",)


@pytest.mark.parametrize("cambio", [{"extra": 1}, {"reglas": []}, {"productoId": "Mayusculas"}])
def test_definiciones_fuera_del_esquema(cambio: dict[str, object]) -> None:
    definicion = dict(semillas(MODELOS)[0][4], **cambio)
    with pytest.raises(ValidationError):
        DefinicionReglasFallback.model_validate(definicion)


def test_una_regla_fuera_del_esquema() -> None:
    definicion = semillas(MODELOS)[0][4]
    for cambio in (
        {"id": "FB-1"},
        {"fuente": "minusculas"},
        {"accion": "RECHAZAR"},
        {"ajuste": "-1"},
        {"ajuste": 1.05},
        {"mensajeCodigo": "LIBRE"},
    ):
        regla = dict(definicion["reglas"][0], **cambio)
        with pytest.raises(ValidationError):
            DefinicionReglasFallback.model_validate(dict(definicion, reglas=[regla]))


def test_la_respuesta_es_la_del_openapi(api: TestClient) -> None:
    respuesta = api.get("/v1/productos/vida-hipotecario/reglas-fallback/vigente").json()
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["ReglasFallbackRespuesta"]["properties"]) == set(respuesta)
    assert set(esquemas["ReglaFallback"]["properties"]) == set(respuesta["definicion"]["reglas"][0])
