"""El modelo de entrada de dinero coincide con `OfertaAceptada.v1` en los campos que usa
(HU-W16, 16.10).

dinero es un lector tolerante: valida solo lo que usa (`claveRecaudo`, `cotizacionId`, `clienteId`
y `prima`, más el sobre) e ignora el resto. Cada campo que lee debe existir en el esquema publicado
por polizas, ser obligatorio allí y tener el mismo tipo, formato y patrón: si polizas cambiara uno,
esta prueba falla antes que el consumidor en producción.
"""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from dinero.adapters.inbound.sqs.oferta_aceptada import SobreOfertaAceptadaV1
from jsonschema import Draft202012Validator

PUBLICADO = json.loads(
    (
        Path(__file__).parents[4] / "contracts" / "events" / "OfertaAceptada.v1.schema.json"
    ).read_text(encoding="utf-8")
)
PROPIO = SobreOfertaAceptadaV1.model_json_schema(by_alias=True)
_RESTRICCIONES = ("type", "format", "pattern", "const", "enum", "anyOf")


def _resolver(esquema: dict[str, Any], nodo: dict[str, Any]) -> dict[str, Any]:
    referencia = nodo.get("$ref")
    if referencia is None:
        return nodo
    resuelto: dict[str, Any] = esquema["$defs"][referencia.split("/")[-1]]
    return resuelto


def _comparar(propio: dict[str, Any], publicado: dict[str, Any], ruta: str) -> list[str]:
    """Diferencias entre lo que dinero exige y lo que polizas publica, campo a campo."""
    diferencias: list[str] = []
    propio, publicado = _resolver(PROPIO, propio), _resolver(PUBLICADO, publicado)
    if "properties" in propio:
        requeridos = set(publicado.get("required", []))
        for nombre, sub in propio["properties"].items():
            if nombre not in publicado.get("properties", {}):
                diferencias.append(f"{ruta}{nombre}: no está en el esquema publicado")
                continue
            if nombre not in requeridos:
                diferencias.append(f"{ruta}{nombre}: opcional en el esquema publicado")
            diferencias += _comparar(sub, publicado["properties"][nombre], f"{ruta}{nombre}.")
        return diferencias
    for clave in _RESTRICCIONES:
        if clave not in propio:
            continue
        if clave == "const":
            # dinero fija el valor (`type` = OfertaAceptada, `version` = 1); el publicado lo
            # declara como const o deja el tipo abierto.
            if "const" in publicado and publicado["const"] != propio["const"]:
                diferencias.append(f"{ruta[:-1]}: const {propio['const']} != {publicado['const']}")
            continue
        if propio[clave] != publicado.get(clave):
            diferencias.append(
                f"{ruta[:-1]}: {clave} {propio[clave]!r} != {publicado.get(clave)!r}"
            )
    return diferencias


def test_cada_campo_que_usa_dinero_coincide_con_el_publicado() -> None:
    assert _comparar(PROPIO, PUBLICADO, "") == []


def test_dinero_lee_los_campos_que_necesita_y_ninguno_mas() -> None:
    payload = _resolver(PROPIO, PROPIO["properties"]["payload"])
    assert sorted(payload["properties"]) == ["claveRecaudo", "clienteId", "cotizacionId", "prima"]
    assert sorted(_resolver(PROPIO, payload["properties"]["prima"])["properties"]) == [
        "moneda",
        "periodicidad",
        "valor",
    ]


def _ejemplo() -> dict[str, Any]:
    aceptacion = str(uuid4())
    return {
        "eventId": str(uuid4()),
        "aggregateId": aceptacion,
        "type": "OfertaAceptada",
        "version": 1,
        "occurredAt": "2026-10-28T13:05:26.000120Z",
        "correlationId": "corr-sintetico-01",
        "payload": {
            "aceptacionId": aceptacion,
            "cotizacionId": str(uuid4()),
            "ofertaId": None,
            "decisionId": str(uuid4()),
            "clienteId": "00000000-0000-4000-8000-000000000001",
            "productoId": "VIDA-INDIVIDUAL",
            "prima": {"valor": "64638.00", "moneda": "COP", "periodicidad": "MENSUAL"},
            "claveRecaudo": aceptacion,
            "huellaCalculo": "sha256:abc",
            "vigenteHasta": "2026-11-27T13:05:26Z",
            "versionDeclaraciones": "2026.10.1",
            "aceptadaEn": "2026-10-28T13:05:26Z",
            "actorId": "asesor-sintetico-01",
            "canal": "PORTAL_ASESOR",
        },
    }


def test_un_evento_que_cumple_el_esquema_publicado_lo_lee_dinero() -> None:
    ejemplo = _ejemplo()
    validador = Draft202012Validator(PUBLICADO, format_checker=Draft202012Validator.FORMAT_CHECKER)
    assert list(validador.iter_errors(ejemplo)) == []
    sobre = SobreOfertaAceptadaV1.model_validate_json(json.dumps(ejemplo))
    assert str(sobre.payload.clave_recaudo) == ejemplo["payload"]["claveRecaudo"]
    assert sobre.payload.prima.valor == "64638.00"


@pytest.mark.parametrize(
    "ruta", [("payload", "claveRecaudo"), ("payload", "prima", "valor"), ("payload", "clienteId")]
)
def test_lo_que_el_esquema_publicado_rechaza_tambien_lo_rechaza_dinero(
    ruta: tuple[str, ...],
) -> None:
    ejemplo = _ejemplo()
    nodo = ejemplo
    for clave in ruta[:-1]:
        nodo = nodo[clave]
    del nodo[ruta[-1]]
    validador = Draft202012Validator(PUBLICADO)
    assert list(validador.iter_errors(ejemplo)) != []
    with pytest.raises(ValueError):
        SobreOfertaAceptadaV1.model_validate_json(json.dumps(ejemplo))
