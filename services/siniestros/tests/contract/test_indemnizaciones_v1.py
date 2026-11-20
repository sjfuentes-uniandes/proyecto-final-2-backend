"""Los modelos de entrada de siniestros coinciden con `IndemnizacionConfirmada.v1` e
`IndemnizacionRechazada.v1` en los campos que usan (HU-W23; DS3-25).

siniestros es un lector tolerante: valida solo lo que usa e ignora el resto. Cada campo que lee
debe existir en el esquema publicado por `dinero`, ser obligatorio allí y tener el mismo tipo,
formato, patrón y límites: si `dinero` cambiara uno, esta prueba falla antes que el consumidor en
producción. Ningún esquema trae datos de la cuenta del beneficiario.
"""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from siniestros.adapters.inbound.sqs.indemnizaciones import (
    SobreConfirmadaV1,
    SobreRechazadaV1,
)

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
_RESTRICCIONES = (
    "type",
    "format",
    "pattern",
    "const",
    "enum",
    "anyOf",
    "minLength",
    "maxLength",
    "minimum",
)
CASOS = {
    "IndemnizacionConfirmada": SobreConfirmadaV1,
    "IndemnizacionRechazada": SobreRechazadaV1,
}


def _publicado(tipo: str) -> dict[str, Any]:
    return json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))


def _resolver(esquema: dict[str, Any], nodo: dict[str, Any]) -> dict[str, Any]:
    referencia = nodo.get("$ref")
    if referencia is None:
        return nodo
    resuelto: dict[str, Any] = esquema["$defs"][referencia.split("/")[-1]]
    return resuelto


def _comparar(
    propio_raiz: dict[str, Any],
    publicado_raiz: dict[str, Any],
    propio: dict[str, Any],
    publicado: dict[str, Any],
    ruta: str,
) -> list[str]:
    diferencias: list[str] = []
    propio, publicado = _resolver(propio_raiz, propio), _resolver(publicado_raiz, publicado)
    if "properties" in propio:
        requeridos = set(publicado.get("required", []))
        for nombre, sub in propio["properties"].items():
            if nombre not in publicado.get("properties", {}):
                diferencias.append(f"{ruta}{nombre}: no está en el esquema publicado")
                continue
            if nombre not in requeridos:
                diferencias.append(f"{ruta}{nombre}: opcional en el esquema publicado")
            diferencias += _comparar(
                propio_raiz,
                publicado_raiz,
                sub,
                publicado["properties"][nombre],
                f"{ruta}{nombre}.",
            )
        return diferencias
    for clave in _RESTRICCIONES:
        if clave not in propio:
            continue
        if clave == "const":
            if "const" in publicado and publicado["const"] != propio["const"]:
                diferencias.append(f"{ruta[:-1]}: const {propio['const']} != {publicado['const']}")
            continue
        if propio[clave] != publicado.get(clave):
            diferencias.append(
                f"{ruta[:-1]}: {clave} {propio[clave]!r} != {publicado.get(clave)!r}"
            )
    return diferencias


@pytest.mark.parametrize("tipo", CASOS)
def test_cada_campo_que_usa_siniestros_coincide_con_el_publicado(tipo: str) -> None:
    propio = CASOS[tipo].model_json_schema(by_alias=True)
    assert _comparar(propio, _publicado(tipo), propio, _publicado(tipo), "") == []


def test_siniestros_lee_lo_que_necesita_y_ninguna_cuenta() -> None:
    campos = {
        tipo: sorted(
            _resolver(
                m.model_json_schema(by_alias=True),
                m.model_json_schema(by_alias=True)["properties"]["payload"],
            )["properties"]
        )
        for tipo, m in CASOS.items()
    }
    assert campos["IndemnizacionConfirmada"] == [
        "confirmadaEn",
        "duplicadosDescartados",
        "intentos",
        "referencia",
        "siniestroId",
        "transaccionId",
    ]
    assert campos["IndemnizacionRechazada"] == [
        "duplicadosDescartados",
        "intentos",
        "motivo",
        "rechazadaEn",
        "referencia",
        "siniestroId",
    ]
    for tipo in CASOS:
        publicado = _resolver(_publicado(tipo), _publicado(tipo)["properties"]["payload"])
        assert not {"beneficiarioRef", "cuenta", "cuentaMascarada", "iban"} & set(
            publicado["properties"]
        )


def _ejemplo(tipo: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "indemnizacionId": str(uuid4()),
        "siniestroId": str(uuid4()),
        "referencia": "IND-88-2026-0442-01",
        "intentos": 1,
        "duplicadosDescartados": 0,
    }
    if tipo == "IndemnizacionConfirmada":
        payload |= {
            "monto": "2500000.00",
            "moneda": "COP",
            "transaccionId": "TRX-1",
            "confirmadaEn": "2026-11-10T15:00:03.000120Z",
        }
    else:
        payload |= {"motivo": "BENEFICIARIO_NO_HABILITADO", "rechazadaEn": "2026-11-10T15:00:03Z"}
    return {
        "eventId": str(uuid4()),
        "aggregateId": str(uuid4()),
        "type": tipo,
        "version": 1,
        "occurredAt": "2026-11-10T15:00:03Z",
        "correlationId": "corr-sintetico-01",
        "payload": payload,
    }


@pytest.mark.parametrize("tipo", CASOS)
def test_un_evento_que_cumple_el_esquema_publicado_lo_lee_siniestros(tipo: str) -> None:
    ejemplo = _ejemplo(tipo)
    validador = Draft202012Validator(
        _publicado(tipo), format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    assert list(validador.iter_errors(ejemplo)) == []
    sobre = CASOS[tipo].model_validate_json(json.dumps(ejemplo))
    assert str(sobre.payload.siniestro_id) == ejemplo["payload"]["siniestroId"]


@pytest.mark.parametrize(
    ("tipo", "campo"),
    [
        ("IndemnizacionConfirmada", "siniestroId"),
        ("IndemnizacionConfirmada", "referencia"),
        ("IndemnizacionConfirmada", "transaccionId"),
        ("IndemnizacionConfirmada", "intentos"),
        ("IndemnizacionConfirmada", "duplicadosDescartados"),
        ("IndemnizacionConfirmada", "confirmadaEn"),
        ("IndemnizacionRechazada", "motivo"),
        ("IndemnizacionRechazada", "rechazadaEn"),
    ],
)
def test_lo_que_el_esquema_publicado_rechaza_tambien_lo_rechaza_siniestros(
    tipo: str, campo: str
) -> None:
    ejemplo = _ejemplo(tipo)
    del ejemplo["payload"][campo]
    assert list(Draft202012Validator(_publicado(tipo)).iter_errors(ejemplo)) != []
    with pytest.raises(ValueError):
        CASOS[tipo].model_validate_json(json.dumps(ejemplo))
