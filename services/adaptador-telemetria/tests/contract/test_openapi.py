"""El contrato publicado (contracts/openapi/adaptador-telemetria.yaml) es el que sirve la app.

Si cambia la API, regenerar con:
    uv run python -c "import yaml; from adaptador_telemetria.main import app; \
open('contracts/openapi/adaptador-telemetria.yaml', 'w').write(yaml.safe_dump(app.openapi(), \
sort_keys=False, allow_unicode=True))"
"""

from pathlib import Path
from typing import Any

import yaml
from adaptador_telemetria.main import app
from jsonschema import Draft202012Validator

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "adaptador-telemetria.yaml"
RUTA = "/v1/eventos"


def _publicado() -> dict[str, Any]:
    contrato: dict[str, Any] = yaml.safe_load(CONTRATO.read_text(encoding="utf-8"))
    return contrato


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert _publicado() == app.openapi()


def test_contrato_de_la_recepcion_de_eventos() -> None:
    operacion = _publicado()["paths"][RUTA]["post"]
    # DS3-16: 202 nuevo, 200 reenvío, 401 (firma o marca), 422 (contrato) y 503 (dependencia).
    assert set(operacion["responses"]) == {"200", "202", "401", "422", "503"}
    assert {p["name"] for p in operacion["parameters"]} == {
        "X-Telemetria-Fuente",
        "X-Telemetria-Marca-Tiempo",
        "X-Telemetria-Firma",
    }
    # Opcionales: su ausencia es un 401, no un 422 de FastAPI.
    assert not any(p["required"] for p in operacion["parameters"])
    assert operacion["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EventoExterno"
    }
    # Sin JWT: la autenticidad es la firma.
    assert "security" not in operacion and "security" not in _publicado()


def test_el_cuerpo_no_admite_campos_de_mas_y_exige_la_version() -> None:
    esquema = _publicado()["components"]["schemas"]["EventoExterno"]
    assert esquema["additionalProperties"] is False
    assert esquema["properties"]["versionContrato"]["const"] == "1"
    assert set(esquema["required"]) == {
        "versionContrato",
        "idExterno",
        "eventoOrigen",
        "tipoEvento",
        "valorObservado",
        "unidad",
        "zona",
        "observadoEn",
    }


def test_los_casos_aceptados_del_archivo_de_datos_cumplen_el_esquema() -> None:
    """Los envíos válidos del archivo de datos cumplen el esquema publicado; los de contrato
    inválido incumplen el esquema (salvo la unidad incoherente, que valida el dominio)."""
    import json

    datos = json.loads(
        (CONTRATO.parents[1] / "datos-sinteticos" / "eventos-parametricos.json").read_text(
            encoding="utf-8"
        )
    )
    componentes = _publicado()["components"]["schemas"]
    esquema = {
        **componentes["EventoExterno"],
        "$defs": {"Ubicacion": componentes["Ubicacion"]},
    }
    esquema = json.loads(json.dumps(esquema).replace("#/components/schemas/", "#/$defs/"))
    validador = Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    for caso in datos["eventos"]:
        cuerpo = caso["envio"].get("cuerpo")
        if cuerpo is None:
            continue
        invalido = caso["esperado"]["http"]["codigo"] == "CONTRATO_INVALIDO"
        incoherente = caso["clave"] == "unidadIncoherente"
        errores = list(validador.iter_errors(cuerpo))
        assert (not errores) is (not invalido or incoherente), caso["clave"]
