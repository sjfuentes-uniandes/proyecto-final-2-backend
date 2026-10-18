"""CT-W11-02: los esquemas publicados en contracts/events son los que genera el código.

Si cambia un evento, regenerar con: uv run python -m cotizacion.adapters.outbound.eventos
"""

import json
from pathlib import Path

import pytest
from cotizacion.adapters.outbound.eventos import ESQUEMAS
from jsonschema import Draft202012Validator
from pydantic import BaseModel

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"


@pytest.mark.parametrize(("archivo", "modelo"), ESQUEMAS.items())
def test_ct_w11_02_esquema_publicado_coincide(archivo: str, modelo: type[BaseModel]) -> None:
    publicado = json.loads((EVENTOS / archivo).read_text(encoding="utf-8"))
    assert publicado == modelo.model_json_schema(by_alias=True)


def test_ct_w11_02_los_esquemas_del_codigo_estan_publicados() -> None:
    """Otros servicios (clientes) publican sus propios esquemas en el mismo directorio."""
    assert set(ESQUEMAS) <= {p.name for p in EVENTOS.glob("*.json")}


def _validador(nombre: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((EVENTOS / nombre).read_text(encoding="utf-8")))


SOBRE_ANTERIOR = {
    "eventId": "00000000-0000-4000-a000-0000000000e1",
    "aggregateId": "00000000-0000-4000-a000-000000000001",
    "type": "DatosConsentidosConsultados",
    "version": 1,
    "occurredAt": "2026-10-15T15:00:00Z",
    "correlationId": "corr-contrato",
    "payload": {
        "perfilId": "00000000-0000-4000-a000-000000000001",
        "clienteId": "00000000-0000-4000-8000-000000000001",
        "consentimientoId": "00000000-0000-4000-9000-000000000001",
        "proposito": "PERFILAMIENTO_PRECIO",
        "fuentes": [],
    },
}


def test_hu_w31_un_evento_anterior_a_la_comprobacion_sigue_siendo_v1_valido() -> None:
    """Campos opcionales en v1 (P3-6): lo emitido antes de la PR 3.4 cumple el esquema nuevo."""
    validador = _validador("DatosConsentidosConsultados.v1.schema.json")
    validador.validate(SOBRE_ANTERIOR)
    payload = validador.schema["$defs"]["DatosConsentidosConsultadosV1"]
    assert payload["required"] == [
        "perfilId",
        "clienteId",
        "consentimientoId",
        "proposito",
        "fuentes",
    ]


def test_hu_w31_el_evento_con_la_marca_cumple_el_esquema() -> None:
    validador = _validador("DatosConsentidosConsultados.v1.schema.json")
    marcado = {
        **SOBRE_ANTERIOR,
        "payload": {
            **SOBRE_ANTERIOR["payload"],
            "comprobacionFinal": {"decision": "RECHAZADO", "causa": "REVOCADO"},
            "revocadoDurantePerfilamiento": True,
        },
    }
    validador.validate(marcado)
    invalido = {
        **marcado,
        "payload": {**marcado["payload"], "comprobacionFinal": {"decision": "OTRA", "causa": None}},
    }
    assert not validador.is_valid(invalido)
