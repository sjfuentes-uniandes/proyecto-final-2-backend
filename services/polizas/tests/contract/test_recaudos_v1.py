"""Los modelos de entrada de polizas coinciden con `RecaudoConfirmado.v1` y `RecaudoRechazado.v1`
en los campos que usan (HU-W16, 16.13).

polizas es un lector tolerante: valida solo lo que usa (sin `cotizacionId`) e ignora el resto.
Cada campo que lee debe existir en el esquema publicado por dinero, ser obligatorio allí y tener el
mismo tipo, formato, patrón y longitudes: si dinero cambiara uno, esta prueba falla antes que el
consumidor en producción. Y lo que el esquema publicado rechaza en esos campos, polizas también.
"""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from polizas.adapters.inbound.sqs.recaudos import (
    SobreRecaudoConfirmadoV1,
    SobreRecaudoRechazadoV1,
)
from pydantic import BaseModel, ValidationError

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
MODELOS: dict[str, type[BaseModel]] = {
    "RecaudoConfirmado": SobreRecaudoConfirmadoV1,
    "RecaudoRechazado": SobreRecaudoRechazadoV1,
}
_RESTRICCIONES = ("type", "format", "pattern", "const", "enum", "anyOf", "minLength", "maxLength")


def _publicado(tipo: str) -> dict[str, Any]:
    esquema: dict[str, Any] = json.loads(
        (EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8")
    )
    return esquema


def _resolver(esquema: dict[str, Any], nodo: dict[str, Any]) -> dict[str, Any]:
    referencia = nodo.get("$ref")
    if referencia is None:
        return nodo
    resuelto: dict[str, Any] = esquema["$defs"][referencia.split("/")[-1]]
    return resuelto


def _comparar(
    raiz_propia: dict[str, Any],
    raiz_publicada: dict[str, Any],
    propio: dict[str, Any],
    publicado: dict[str, Any],
    ruta: str,
) -> list[str]:
    """Diferencias entre lo que polizas exige y lo que dinero publica, campo a campo."""
    diferencias: list[str] = []
    propio, publicado = _resolver(raiz_propia, propio), _resolver(raiz_publicada, publicado)
    if "properties" in propio:
        requeridos = set(publicado.get("required", []))
        for nombre, sub in propio["properties"].items():
            if nombre not in publicado.get("properties", {}):
                diferencias.append(f"{ruta}{nombre}: no está en el esquema publicado")
                continue
            if nombre not in requeridos:
                diferencias.append(f"{ruta}{nombre}: opcional en el esquema publicado")
            diferencias += _comparar(
                raiz_propia,
                raiz_publicada,
                sub,
                publicado["properties"][nombre],
                f"{ruta}{nombre}.",
            )
        return diferencias
    for clave in _RESTRICCIONES:
        if clave not in propio:
            continue
        if clave == "const":
            # polizas fija el valor (`type` y `version` = 1); el publicado lo declara como const o
            # deja el tipo abierto.
            if "const" in publicado and publicado["const"] != propio["const"]:
                diferencias.append(f"{ruta[:-1]}: const {propio['const']} != {publicado['const']}")
            continue
        if propio[clave] != publicado.get(clave):
            diferencias.append(
                f"{ruta[:-1]}: {clave} {propio[clave]!r} != {publicado.get(clave)!r}"
            )
    return diferencias


@pytest.mark.parametrize("tipo", MODELOS)
def test_cada_campo_que_usa_polizas_coincide_con_el_publicado(tipo: str) -> None:
    propio = MODELOS[tipo].model_json_schema(by_alias=True)
    publicado = _publicado(tipo)
    assert _comparar(propio, publicado, propio, publicado, "") == []


def test_polizas_lee_los_campos_que_necesita_y_ninguno_mas() -> None:
    def campos(modelo: type[BaseModel]) -> list[str]:
        esquema = modelo.model_json_schema(by_alias=True)
        payload = _resolver(esquema, esquema["properties"]["payload"])
        return sorted(payload["properties"])

    assert campos(SobreRecaudoConfirmadoV1) == [
        "claveRecaudo",
        "confirmadoEn",
        "monto",
        "recaudoId",
        "transaccionId",
    ]
    assert campos(SobreRecaudoRechazadoV1) == [
        "claveRecaudo",
        "motivo",
        "recaudoId",
        "rechazadoEn",
    ]


def _ejemplo(tipo: str) -> dict[str, Any]:
    recaudo_id = str(uuid4())
    payload: dict[str, Any] = {
        "recaudoId": recaudo_id,
        "claveRecaudo": str(uuid4()),
        "cotizacionId": str(uuid4()),
    }
    if tipo == "RecaudoConfirmado":
        payload |= {
            "monto": {"valor": "64638.00", "moneda": "COP"},
            "transaccionId": "TX-SINTETICA-0001",
            "confirmadoEn": "2026-10-28T13:05:31.512000Z",
        }
    else:
        payload |= {"motivo": "FONDOS_INSUFICIENTES", "rechazadoEn": "2026-10-28T13:05:31Z"}
    return {
        "eventId": str(uuid4()),
        "aggregateId": recaudo_id,
        "type": tipo,
        "version": 1,
        "occurredAt": "2026-10-28T13:05:31.512000Z",
        "correlationId": "corr-sintetico-01",
        "payload": payload,
    }


@pytest.mark.parametrize("tipo", MODELOS)
def test_un_evento_que_cumple_el_esquema_publicado_lo_lee_polizas(tipo: str) -> None:
    ejemplo = _ejemplo(tipo)
    validador = Draft202012Validator(
        _publicado(tipo), format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    assert list(validador.iter_errors(ejemplo)) == []
    sobre: Any = MODELOS[tipo].model_validate_json(json.dumps(ejemplo))
    assert str(sobre.payload.clave_recaudo) == ejemplo["payload"]["claveRecaudo"]


RECHAZOS = [
    ("RecaudoConfirmado", ("payload", "claveRecaudo"), None),
    ("RecaudoConfirmado", ("payload", "transaccionId"), None),
    ("RecaudoConfirmado", ("payload", "transaccionId"), ""),
    ("RecaudoConfirmado", ("payload", "transaccionId"), "X" * 129),
    ("RecaudoConfirmado", ("payload", "monto", "valor"), "64638.001"),
    ("RecaudoConfirmado", ("payload", "monto", "valor"), 64638),
    ("RecaudoConfirmado", ("payload", "monto", "moneda"), "cop"),
    ("RecaudoConfirmado", ("version",), 2),
    ("RecaudoRechazado", ("payload", "motivo"), None),
    ("RecaudoRechazado", ("payload", "motivo"), "fondos"),
    ("RecaudoRechazado", ("payload", "recaudoId"), None),
]


@pytest.mark.parametrize(("tipo", "ruta", "valor"), RECHAZOS)
def test_lo_que_el_esquema_publicado_rechaza_tambien_lo_rechaza_polizas(
    tipo: str, ruta: tuple[str, ...], valor: Any
) -> None:
    """`valor` None quita el campo; otro valor lo reemplaza."""
    ejemplo = _ejemplo(tipo)
    nodo = ejemplo
    for clave in ruta[:-1]:
        nodo = nodo[clave]
    if valor is None:
        del nodo[ruta[-1]]
    else:
        nodo[ruta[-1]] = valor
    assert list(Draft202012Validator(_publicado(tipo)).iter_errors(ejemplo)) != []
    with pytest.raises(ValidationError):
        MODELOS[tipo].model_validate_json(json.dumps(ejemplo))
