"""CT-W17-01 (evento): el esquema publicado de PolizaEmitida.v1 es el que genera el código, un
evento completo lo cumple y cada desvío del contrato lo incumple (HU-W17, 17.2; ADR 0014,
decisiones 6 y 7).

Todavía no hay productor (17.8): el ejemplo es un sobre armado a mano con datos sintéticos. Se
valida con el verificador de formatos (`uuid`, `date`, `date-time`), como debe hacerlo el
consumidor: sin él, JSON Schema trata `format` como una anotación.

Como los demás eventos, el esquema no prohíbe campos adicionales: v1 solo crece de forma aditiva y
el consumidor ignora lo que no conoce.

Si cambia el evento, regenerar con: uv run python -m polizas.adapters.outbound.eventos
"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from polizas.adapters.outbound.eventos import ESQUEMAS, POLIZA_EMITIDA

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
ARCHIVO = f"{POLIZA_EMITIDA}.v1.schema.json"

_POLIZA_ID = "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"


def _publicado() -> dict[str, Any]:
    esquema: dict[str, Any] = json.loads((EVENTOS / ARCHIVO).read_text(encoding="utf-8"))
    return esquema


def _errores(sobre: dict[str, Any]) -> list[str]:
    validador = Draft202012Validator(
        _publicado(), format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    return [error.message for error in validador.iter_errors(sobre)]


def _ejemplo() -> dict[str, Any]:
    """Un PolizaEmitida completo, con datos sintéticos (W17-4: vigencia 13/09/2026–12/09/2027)."""
    return {
        "eventId": "0b7e4c1a-9f2d-4a3b-8c5d-6e7f8a9b0c1d",
        "aggregateId": _POLIZA_ID,
        "type": "PolizaEmitida",
        "version": 1,
        "occurredAt": "2026-09-12T15:04:05.123000-05:00",
        "correlationId": "corr-emision",
        "payload": {
            "polizaId": _POLIZA_ID,
            "numero": "100-2026-004817",
            "cotizacionId": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "claveRecaudo": "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e",
            "clienteId": "3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f",
            "producto": {"id": "vida-hipotecario", "version": "2026.1"},
            "vigencia": {"desde": "2026-09-13", "hasta": "2027-09-12"},
            "prima": "247380",
            "sumaAsegurada": "180000000.00",
            "moneda": "COP",
            "periodicidad": "MENSUAL",
            "documento": {
                "clave": f"polizas/{_POLIZA_ID}/poliza.pdf",
                "versionId": "3HL4kqtJlcpXroDTDmJ+rmSpXd3dIbrHY+MTRCxf3vjVBH40Nr8X8gdRQBpUMLUo",
                "huella": "sha256:" + "a1" * 32,
                "tamanoBytes": 48213,
            },
            "firma": {"firmaId": "FIR-SINT-0001", "selloTiempo": "2026-09-12T20:04:05Z"},
            "emitidaEn": "2026-09-12T15:04:05.123000-05:00",
        },
    }


def test_esquema_publicado_coincide() -> None:
    assert ARCHIVO in ESQUEMAS
    assert _publicado() == ESQUEMAS[ARCHIVO].model_json_schema(by_alias=True)


def test_el_ejemplo_completo_cumple_el_esquema() -> None:
    assert _errores(_ejemplo()) == []


def test_el_sobre_fija_el_tipo_y_la_version() -> None:
    propiedades = _publicado()["properties"]
    assert propiedades["type"]["const"] == "PolizaEmitida"
    assert propiedades["version"]["const"] == 1
    assert set(_publicado()["required"]) == {
        "eventId",
        "aggregateId",
        "type",
        "version",
        "occurredAt",
        "correlationId",
        "payload",
    }


def test_el_payload_exige_todos_sus_campos() -> None:
    """`documento` y `firma` siempre van: el evento se emite con la póliza ya ACTIVA."""
    ejemplo = _ejemplo()
    for campo in ejemplo["payload"]:
        sobre = copy.deepcopy(ejemplo)
        del sobre["payload"][campo]
        assert _errores(sobre), f"aceptó el payload sin {campo}"
    for grupo in ("producto", "vigencia", "documento", "firma"):
        for campo in ejemplo["payload"][grupo]:
            sobre = copy.deepcopy(ejemplo)
            del sobre["payload"][grupo][campo]
            assert _errores(sobre), f"aceptó {grupo} sin {campo}"


def _sin_version_del_documento(p: dict[str, Any]) -> None:
    del p["documento"]["versionId"]


def _prima_como_numero(p: dict[str, Any]) -> None:
    p["prima"] = 247380.5


def _suma_con_tres_decimales(p: dict[str, Any]) -> None:
    p["sumaAsegurada"] = "180000000.001"


def _huella_sin_prefijo(p: dict[str, Any]) -> None:
    p["documento"]["huella"] = "a1" * 32


def _numero_con_otro_formato(p: dict[str, Any]) -> None:
    p["numero"] = "VH-2026-4817"


def _vigencia_con_hora(p: dict[str, Any]) -> None:
    p["vigencia"]["desde"] = "2026-09-13T00:00:00-05:00"


def _tamano_cero(p: dict[str, Any]) -> None:
    p["documento"]["tamanoBytes"] = 0


def _emitida_sin_zona(p: dict[str, Any]) -> None:
    p["emitidaEn"] = "2026-09-12T15:04:05"


def _sello_sin_zona(p: dict[str, Any]) -> None:
    p["firma"]["selloTiempo"] = "2026-09-12T20:04:05"


def _cliente_no_uuid(p: dict[str, Any]) -> None:
    p["clienteId"] = "C01"


@pytest.mark.parametrize(
    "desvio",
    [
        _sin_version_del_documento,
        _prima_como_numero,
        _suma_con_tres_decimales,
        _huella_sin_prefijo,
        _numero_con_otro_formato,
        _vigencia_con_hora,
        _tamano_cero,
        _emitida_sin_zona,
        _sello_sin_zona,
        _cliente_no_uuid,
    ],
    ids=lambda desvio: desvio.__name__.lstrip("_"),
)
def test_el_esquema_rechaza_cada_desvio(desvio: Callable[[dict[str, Any]], None]) -> None:
    sobre = _ejemplo()
    desvio(sobre["payload"])
    assert _errores(sobre)


@pytest.mark.parametrize(("campo", "valor"), [("type", "PolizaEmitidaV2"), ("version", 2)])
def test_el_esquema_rechaza_otro_tipo_u_otra_version(campo: str, valor: object) -> None:
    sobre = _ejemplo()
    sobre[campo] = valor
    assert _errores(sobre)


def test_un_campo_adicional_se_acepta_como_en_los_demas_eventos() -> None:
    """Ningún evento publicado declara `additionalProperties: false`: lector tolerante."""
    sobre = _ejemplo()
    sobre["payload"]["campoFuturo"] = "x"
    sobre["payload"]["documento"]["campoFuturo"] = "x"
    assert _errores(sobre) == []
    assert "additionalProperties" not in json.dumps(_publicado())


def test_el_payload_no_lleva_nombres_ni_documentos_de_identidad_ni_contacto() -> None:
    payload = _publicado()["$defs"]["PolizaEmitidaV1"]["properties"]
    prohibidos = {
        "nombre",
        "nombreCompleto",
        "titularNombre",
        "canalNombre",
        "clienteNombre",
        "tipoDocumento",
        "numeroDocumento",
        "fechaNacimiento",
        "correo",
        "telefono",
        "direccion",
        "medioPago",
        "cuenta",
        "tarjeta",
    }
    assert not prohibidos & set(payload)
