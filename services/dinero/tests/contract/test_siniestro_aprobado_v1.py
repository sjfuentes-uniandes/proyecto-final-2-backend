"""El modelo de entrada de dinero coincide con `SiniestroAprobado.v1` en los campos que usa
(HU-W23; ADR 0015).

dinero es un lector tolerante: valida solo lo que usa (`siniestroId`, `valorIndemnizar`, `moneda`,
`referenciaPago` y `beneficiarioRef`, más el sobre) e ignora el resto. Cada campo que lee debe
existir en el esquema publicado por `siniestros`, ser obligatorio allí y tener el mismo tipo,
formato y patrón: si `siniestros` cambiara uno, esta prueba falla antes que el consumidor en
producción. `beneficiarioRef` es una referencia opaca: el esquema no tiene ningún campo de cuenta.
"""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from dinero.adapters.inbound.sqs.siniestro_aprobado import SobreSiniestroAprobadoV1
from jsonschema import Draft202012Validator

PUBLICADO = json.loads(
    (
        Path(__file__).parents[4] / "contracts" / "events" / "SiniestroAprobado.v1.schema.json"
    ).read_text(encoding="utf-8")
)
PROPIO = SobreSiniestroAprobadoV1.model_json_schema(by_alias=True)
_RESTRICCIONES = ("type", "format", "pattern", "const", "enum", "anyOf", "minLength", "maxLength")


def _resolver(esquema: dict[str, Any], nodo: dict[str, Any]) -> dict[str, Any]:
    referencia = nodo.get("$ref")
    if referencia is None:
        return nodo
    resuelto: dict[str, Any] = esquema["$defs"][referencia.split("/")[-1]]
    return resuelto


def _comparar(propio: dict[str, Any], publicado: dict[str, Any], ruta: str) -> list[str]:
    """Diferencias entre lo que dinero exige y lo que siniestros publica, campo a campo."""
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
            if "const" in publicado and publicado["const"] != propio["const"]:
                diferencias.append(f"{ruta[:-1]}: const {propio['const']} != {publicado['const']}")
            continue
        if propio[clave] != publicado.get(clave):
            diferencias.append(
                f"{ruta[:-1]}: {clave} {propio[clave]!r} != {publicado.get(clave)!r}"
            )
    return diferencias


def test_cada_campo_que_usa_dinero_coincide_con_el_publicado() -> None:
    # `valorIndemnizar` y `referenciaPago`: dinero es más estricto en la referencia (cualquier `-NN`
    # lo admite el esquema propio de adaptador-pagos, pero la clave de la orden es `-01`), y los
    # patrones son los mismos.
    assert _comparar(PROPIO, PUBLICADO, "") == []


def test_dinero_lee_los_campos_que_necesita_y_ninguno_mas() -> None:
    payload = _resolver(PROPIO, PROPIO["properties"]["payload"])
    assert sorted(payload["properties"]) == [
        "beneficiarioRef",
        "moneda",
        "referenciaPago",
        "siniestroId",
        "valorIndemnizar",
    ]


def test_el_esquema_publicado_no_trae_ningun_dato_de_cuenta() -> None:
    campos = set(_resolver(PUBLICADO, PUBLICADO["properties"]["payload"])["properties"])
    assert not {"cuenta", "cuentaMascarada", "numeroCuenta", "iban", "titular"} & campos


def _ejemplo() -> dict[str, Any]:
    siniestro = str(uuid4())
    return {
        "eventId": str(uuid4()),
        "aggregateId": siniestro,
        "type": "SiniestroAprobado",
        "version": 1,
        "occurredAt": "2026-11-10T15:00:00.000120Z",
        "correlationId": "corr-sintetico-01",
        "payload": {
            "siniestroId": siniestro,
            "codigo": "88-2026-0442",
            "origen": "PORTAL",
            "polizaId": str(uuid4()),
            "clienteId": str(uuid4()),
            "valorIndemnizar": "2500000.00",
            "moneda": "COP",
            "referenciaPago": "IND-88-2026-0442-01",
            "beneficiarioRef": "ben-sint-001",
            "aprobadoEn": "2026-11-10T15:00:00Z",
            "decididoPor": str(uuid4()),
        },
    }


def test_un_evento_que_cumple_el_esquema_publicado_lo_lee_dinero() -> None:
    ejemplo = _ejemplo()
    validador = Draft202012Validator(PUBLICADO, format_checker=Draft202012Validator.FORMAT_CHECKER)
    assert list(validador.iter_errors(ejemplo)) == []
    sobre = SobreSiniestroAprobadoV1.model_validate_json(json.dumps(ejemplo))
    assert str(sobre.payload.siniestro_id) == ejemplo["payload"]["siniestroId"]
    assert sobre.payload.referencia_pago == "IND-88-2026-0442-01"
    assert sobre.payload.beneficiario_ref == "ben-sint-001"


def test_un_paramétrico_sin_decidido_por_tambien_lo_lee_dinero() -> None:
    """DS3-24: el evento es único para R5 y R7; `decididoPor` es null si aprueba un paramétrico."""
    ejemplo = _ejemplo()
    ejemplo["payload"]["origen"] = "PARAMETRICO"
    ejemplo["payload"]["decididoPor"] = None
    validador = Draft202012Validator(PUBLICADO, format_checker=Draft202012Validator.FORMAT_CHECKER)
    assert list(validador.iter_errors(ejemplo)) == []
    SobreSiniestroAprobadoV1.model_validate_json(json.dumps(ejemplo))


@pytest.mark.parametrize(
    "ruta",
    [
        ("payload", "siniestroId"),
        ("payload", "valorIndemnizar"),
        ("payload", "moneda"),
        ("payload", "referenciaPago"),
        ("payload", "beneficiarioRef"),
    ],
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
        SobreSiniestroAprobadoV1.model_validate_json(json.dumps(ejemplo))
