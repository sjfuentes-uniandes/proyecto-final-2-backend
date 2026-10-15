"""El esquema publicado (contracts/modelos/modelo-riesgo.v1.schema.json) es el que genera el código.

Si cambia la definición, regenerar con el comando del docstring de
catalogo.adapters.inbound.http.esquema_modelo. Los casos de rechazo están en
tests/unit/reglas/test_esquema_modelo.py.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from catalogo.adapters.inbound.http.esquema_modelo import DefinicionModelo, a_dict
from pydantic import ValidationError

ESQUEMA = Path(__file__).parents[4] / "contracts" / "modelos" / "modelo-riesgo.v1.schema.json"


def test_el_modelo_candidato_cumple_el_esquema(definicion_candidata: dict[str, Any]) -> None:
    definicion = DefinicionModelo.model_validate(definicion_candidata)
    assert a_dict(definicion) == definicion_candidata


def test_un_campo_extra_se_rechaza(definicion_candidata: dict[str, Any]) -> None:
    definicion_candidata["extra"] = 1
    with pytest.raises(ValidationError):
        DefinicionModelo.model_validate(definicion_candidata)


def test_esquema_publicado_coincide_con_el_generado() -> None:
    assert json.loads(ESQUEMA.read_text(encoding="utf-8")) == DefinicionModelo.model_json_schema()
