"""Los esquemas publicados de `ConsentimientoOtorgado` y `ConsentimientoRevocado` v1 son los que
genera el código y lo que el outbox escribe los cumple (ADR 0009).

Si cambia el evento, regenerar con: uv run python -m clientes.adapters.outbound.eventos
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from clientes.adapters.outbound.eventos import (
    ESQUEMAS,
    SobreConsentimientoOtorgado,
    SobreConsentimientoRevocado,
)
from clientes.adapters.outbound.outbox_pg import consentimiento_otorgado, consentimiento_revocado
from clientes.domain.consentimientos.modelos import ALCANCES, Consentimiento, EstadoConsentimiento
from jsonschema import Draft202012Validator, ValidationError
from pydantic import BaseModel

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
ARCHIVO = "ConsentimientoOtorgado.v1.schema.json"
ARCHIVO_REVOCADO = "ConsentimientoRevocado.v1.schema.json"
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


def _sobre(
    evento: Any, *, con_version_de_agregado: bool, version_de_agregado: int = 1
) -> dict[str, Any]:
    """Lo que el relay publicará a partir de la fila del outbox. `aggregateVersion` no se
    guarda: se deriva del tipo (ADR 0009, Otorgado = 1, Revocado = 2)."""
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
        sobre["aggregateVersion"] = version_de_agregado
    return sobre


@pytest.fixture(scope="module")
def validador() -> Draft202012Validator:
    esquema = json.loads((EVENTOS / ARCHIVO).read_text(encoding="utf-8"))
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


@pytest.mark.parametrize(("archivo", "modelo"), ESQUEMAS.items())
def test_el_esquema_publicado_coincide_con_el_codigo(archivo: str, modelo: type[BaseModel]) -> None:
    publicado = json.loads((EVENTOS / archivo).read_text(encoding="utf-8"))
    assert publicado == modelo.model_json_schema(by_alias=True)


def test_los_esquemas_de_clientes_son_otorgado_y_revocado() -> None:
    assert set(ESQUEMAS) == {ARCHIVO, ARCHIVO_REVOCADO}


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


# --- ConsentimientoRevocado v1 (HU-M09) ---------------------------------------------------------

REVOCADO_EN = datetime(2026, 11, 2, 8, 15, tzinfo=UTC)
REVOCADO = replace(CONSENTIMIENTO, estado=EstadoConsentimiento.REVOCADO, revocado_en=REVOCADO_EN)


@pytest.fixture(scope="module")
def validador_revocado() -> Draft202012Validator:
    esquema = json.loads((EVENTOS / ARCHIVO_REVOCADO).read_text(encoding="utf-8"))
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _sobre_revocado(*, con_version_de_agregado: bool) -> dict[str, Any]:
    evento = consentimiento_revocado(REVOCADO, "sub-titular", "corr-contrato")
    return _sobre(evento, con_version_de_agregado=con_version_de_agregado, version_de_agregado=2)


@pytest.mark.parametrize("con_version_de_agregado", [True, False])
def test_lo_que_escribe_el_outbox_al_revocar_cumple_el_esquema(
    validador_revocado: Draft202012Validator, con_version_de_agregado: bool
) -> None:
    sobre = _sobre_revocado(con_version_de_agregado=con_version_de_agregado)
    validador_revocado.validate(sobre)
    modelo = SobreConsentimientoRevocado.model_validate(sobre)
    assert modelo.aggregate_version == 2  # opcional: si falta, vale 2
    assert sobre["aggregateId"] == str(CONSENTIMIENTO.id)
    assert sobre["occurredAt"] == "2026-11-02T08:15:00Z"


def test_el_payload_de_revocado_lleva_solo_identificadores_y_la_fecha() -> None:
    evento = consentimiento_revocado(REVOCADO, "sub-titular", "corr-contrato")
    assert set(evento.payload) == {"consentimientoId", "clienteId", "revocadoEn", "actorId"}
    assert evento.payload["revocadoEn"] == "2026-11-02T08:15:00+00:00"


@pytest.mark.parametrize("version", [1, 3])
def test_aggregate_version_de_revocado_solo_puede_ser_dos(
    validador_revocado: Draft202012Validator, version: int
) -> None:
    with pytest.raises(ValidationError):
        validador_revocado.validate(
            {**_sobre_revocado(con_version_de_agregado=True), "aggregateVersion": version}
        )


@pytest.mark.parametrize(
    "cambio", [{"type": "ConsentimientoOtorgado"}, {"version": 2}, {"payload": {}}]
)
def test_el_tipo_la_version_y_el_payload_de_revocado_estan_fijados(
    validador_revocado: Draft202012Validator, cambio: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        validador_revocado.validate({**_sobre_revocado(con_version_de_agregado=False), **cambio})


def test_los_eventos_no_se_confunden_entre_si(
    validador: Draft202012Validator, validador_revocado: Draft202012Validator
) -> None:
    """Un consumidor que valida por `type` no acepta un sobre con el esquema del otro."""
    otorgado = _sobre(
        consentimiento_otorgado(CONSENTIMIENTO, "sub", "corr"), con_version_de_agregado=True
    )
    with pytest.raises(ValidationError):
        validador_revocado.validate(otorgado)
    with pytest.raises(ValidationError):
        validador.validate(_sobre_revocado(con_version_de_agregado=True))


def test_el_sobre_comun_es_el_mismo_en_ambos_eventos() -> None:
    """Mismo sobre (ADR 0009): solo cambian `type`, `aggregateVersion` y `payload`."""
    otorgado = json.loads((EVENTOS / ARCHIVO).read_text(encoding="utf-8"))
    revocado = json.loads((EVENTOS / ARCHIVO_REVOCADO).read_text(encoding="utf-8"))
    assert otorgado["required"] == revocado["required"]
    for campo in set(otorgado["properties"]) - {"type", "aggregateVersion", "payload"}:
        assert otorgado["properties"][campo] == revocado["properties"][campo]
