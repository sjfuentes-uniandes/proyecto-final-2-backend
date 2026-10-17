"""El esquema publicado de `ConsentimientoOtorgado` v1 es el que genera el código y lo que el
outbox escribe lo cumple (ADR 0009).

Si cambia el evento, regenerar con: uv run python -m clientes.adapters.outbound.eventos
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from clientes.adapters.outbound.eventos import ESQUEMAS, SobreConsentimientoOtorgado
from clientes.adapters.outbound.outbox_pg import consentimiento_otorgado
from clientes.domain.consentimientos.modelos import ALCANCES, Consentimiento, EstadoConsentimiento
from jsonschema import Draft202012Validator, ValidationError
from pydantic import BaseModel

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
ARCHIVO = "ConsentimientoOtorgado.v1.schema.json"
OTORGADO_EN = datetime(2026, 10, 17, 12, 30, tzinfo=UTC)
CONSENTIMIENTO = Consentimiento(
    id=uuid4(),
    cliente_id=uuid4(),
    proposito="PERFILAMIENTO_PRECIO",
    alcances=tuple(sorted(ALCANCES)),
    version_texto="2026-10-17.1",
    idioma="es-CO",
    estado=EstadoConsentimiento.ACTIVO,
    otorgado_en=OTORGADO_EN,
    vigente_hasta=datetime(2027, 10, 17, 12, 30, tzinfo=UTC),
    revocado_en=None,
    leido_en=OTORGADO_EN,
)


def _sobre(evento: Any, *, con_version_de_agregado: bool) -> dict[str, Any]:
    """Lo que el relay publicará a partir de la fila del outbox. `aggregateVersion` no se
    guarda: se deriva del tipo (ADR 0009, Otorgado = 1)."""
    sobre = {
        "eventId": str(evento.event_id),
        "aggregateId": str(evento.aggregate_id),
        "type": evento.event_type,
        "version": evento.event_version,
        "occurredAt": evento.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "correlationId": evento.correlation_id,
        "payload": evento.payload,
    }
    if con_version_de_agregado:
        sobre["aggregateVersion"] = 1
    return sobre


@pytest.fixture(scope="module")
def validador() -> Draft202012Validator:
    esquema = json.loads((EVENTOS / ARCHIVO).read_text(encoding="utf-8"))
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


@pytest.mark.parametrize(("archivo", "modelo"), ESQUEMAS.items())
def test_el_esquema_publicado_coincide_con_el_codigo(archivo: str, modelo: type[BaseModel]) -> None:
    publicado = json.loads((EVENTOS / archivo).read_text(encoding="utf-8"))
    assert publicado == modelo.model_json_schema(by_alias=True)


def test_solo_hay_un_esquema_de_clientes() -> None:
    assert set(ESQUEMAS) == {ARCHIVO}


@pytest.mark.parametrize("con_version_de_agregado", [True, False])
def test_lo_que_escribe_el_outbox_cumple_el_esquema(
    validador: Draft202012Validator, con_version_de_agregado: bool
) -> None:
    """`aggregateVersion` es opcional: el sobre es válido con y sin él."""
    evento = consentimiento_otorgado(CONSENTIMIENTO, "sub-titular", "corr-contrato")
    sobre = _sobre(evento, con_version_de_agregado=con_version_de_agregado)
    validador.validate(sobre)
    SobreConsentimientoOtorgado.model_validate(sobre)
    assert evento.aggregate_id == CONSENTIMIENTO.id


def test_el_payload_lleva_solo_identificadores_y_codigos() -> None:
    evento = consentimiento_otorgado(CONSENTIMIENTO, "sub-titular", "corr-contrato")
    assert set(evento.payload) == {
        "consentimientoId",
        "clienteId",
        "proposito",
        "alcances",
        "versionTexto",
        "otorgadoEn",
        "vigenteHasta",
        "actorId",
    }
    assert evento.payload["otorgadoEn"] == "2026-10-17T12:30:00+00:00"


def test_aggregate_version_de_otorgado_solo_puede_ser_uno(
    validador: Draft202012Validator,
) -> None:
    evento = consentimiento_otorgado(CONSENTIMIENTO, "sub-titular", "corr-contrato")
    sobre = {**_sobre(evento, con_version_de_agregado=True), "aggregateVersion": 2}
    with pytest.raises(ValidationError):
        validador.validate(sobre)


@pytest.mark.parametrize(
    "falta", ["eventId", "aggregateId", "type", "version", "occurredAt", "correlationId", "payload"]
)
def test_los_campos_del_sobre_son_obligatorios(validador: Draft202012Validator, falta: str) -> None:
    sobre = _sobre(
        consentimiento_otorgado(CONSENTIMIENTO, "sub", "corr"), con_version_de_agregado=True
    )
    del sobre[falta]
    with pytest.raises(ValidationError):
        validador.validate(sobre)


def test_el_tipo_y_la_version_estan_fijados(validador: Draft202012Validator) -> None:
    evento = consentimiento_otorgado(CONSENTIMIENTO, "sub", "corr")
    for cambio in ({"type": "ConsentimientoRevocado"}, {"version": 2}):
        with pytest.raises(ValidationError):
            validador.validate({**_sobre(evento, con_version_de_agregado=False), **cambio})


def test_un_consentimiento_sin_alcances_no_es_un_evento_valido(
    validador: Draft202012Validator,
) -> None:
    evento = consentimiento_otorgado(replace(CONSENTIMIENTO, alcances=()), "sub", "corr")
    with pytest.raises(ValidationError):
        validador.validate(_sobre(evento, con_version_de_agregado=False))
