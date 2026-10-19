"""El esquema publicado de `VerificacionConsentimientoRegistrada` v1 es el que genera el código y
lo que el outbox escribe lo cumple (HU-W31; ADR 0009).

Si cambia el evento, regenerar con: uv run python -m clientes.adapters.outbound.eventos
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from clientes.adapters.outbound.eventos import (
    ESQUEMAS_VERIFICACION,
    SobreVerificacionConsentimientoRegistrada,
)
from clientes.adapters.outbound.outbox_pg import EventoOutbox, verificacion_registrada
from clientes.domain.consentimientos.modelos import Consentimiento, EstadoConsentimiento
from clientes.domain.consentimientos.verificacion import (
    LecturaVerificacion,
    SolicitudVerificacion,
    decidir,
)
from jsonschema import Draft202012Validator, ValidationError

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
ARCHIVO = "VerificacionConsentimientoRegistrada.v1.schema.json"
AHORA = datetime(2026, 10, 18, 9, 30, tzinfo=UTC)
CUATRO = (
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
)
CLIENTE = uuid4()


def _consentimiento(**cambios: Any) -> Consentimiento:
    datos: dict[str, Any] = {
        "id": uuid4(),
        "cliente_id": CLIENTE,
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": CUATRO[:3],
        "version_texto": "v1",
        "idioma": "es-CO",
        "estado": EstadoConsentimiento.ACTIVO,
        "otorgado_en": AHORA - timedelta(days=30),
        "vigente_hasta": AHORA + timedelta(days=150),
        "revocado_en": None,
        "leido_en": AHORA,
    } | cambios
    return Consentimiento(**datos)


def _evento(consentimiento: Consentimiento | None) -> EventoOutbox:
    lectura = LecturaVerificacion(CLIENTE, AHORA, consentimiento, ())
    solicitud = SolicitudVerificacion.crear("PERFILAMIENTO_PRECIO", CUATRO)
    return verificacion_registrada(decidir(solicitud, lectura), "corr-w31")


def _sobre(evento: EventoOutbox) -> dict[str, Any]:
    """Lo que publicará el relay (PR 2.2) a partir de la fila del outbox."""
    return {
        "eventId": str(evento.event_id),
        "aggregateId": str(evento.aggregate_id),
        "type": evento.event_type,
        "version": evento.event_version,
        "occurredAt": evento.occurred_at.isoformat().replace("+00:00", "Z"),
        "correlationId": evento.correlation_id,
        "payload": evento.payload,
    }


@pytest.fixture(scope="module")
def validador() -> Draft202012Validator:
    esquema = json.loads((EVENTOS / ARCHIVO).read_text(encoding="utf-8"))
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def test_el_esquema_publicado_coincide_con_el_codigo() -> None:
    assert ESQUEMAS_VERIFICACION == {ARCHIVO: SobreVerificacionConsentimientoRegistrada}
    publicado = json.loads((EVENTOS / ARCHIVO).read_text(encoding="utf-8"))
    assert publicado == SobreVerificacionConsentimientoRegistrada.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    "consentimiento",
    [
        _consentimiento(),
        _consentimiento(alcances=CUATRO),
        _consentimiento(estado=EstadoConsentimiento.REVOCADO, revocado_en=AHORA),
        _consentimiento(vigente_hasta=AHORA),
        None,
    ],
    ids=["parcial", "permitido", "revocado", "vencido", "sin-consentimiento"],
)
def test_lo_que_escribe_el_outbox_cumple_el_esquema(
    validador: Draft202012Validator, consentimiento: Consentimiento | None
) -> None:
    sobre = _sobre(_evento(consentimiento))
    validador.validate(sobre)
    SobreVerificacionConsentimientoRegistrada.model_validate(sobre)


def test_el_agregado_es_el_cliente_y_no_lleva_version_de_agregado() -> None:
    """ADR 0009: las verificaciones se encadenan por `clienteId`, fuera de la cadena del
    consentimiento."""
    assert _evento(_consentimiento()).aggregate_id == CLIENTE
    publicado = json.loads((EVENTOS / ARCHIVO).read_text(encoding="utf-8"))
    assert "aggregateVersion" not in publicado["properties"]


def test_el_payload_lleva_solo_identificadores_codigos_e_instantes() -> None:
    """Sin datos personales ni valores financieros (W31 AC3)."""
    assert set(_evento(None).payload) == {
        "clienteId",
        "consentimientoId",
        "proposito",
        "alcancesSolicitados",
        "decision",
        "causa",
        "alcancesRechazados",
        "verificadoEn",
    }


@pytest.mark.parametrize(
    ("campo", "valor"),
    [("decision", "permitido"), ("causa", "OTRA"), ("alcancesSolicitados", [])],
)
def test_valores_fuera_del_contrato_no_son_un_evento_valido(
    validador: Draft202012Validator, campo: str, valor: Any
) -> None:
    sobre = _sobre(_evento(None))
    sobre["payload"][campo] = valor
    with pytest.raises(ValidationError):
        validador.validate(sobre)


@pytest.mark.parametrize("falta", ["eventId", "aggregateId", "type", "version", "payload"])
def test_los_campos_del_sobre_son_obligatorios(validador: Draft202012Validator, falta: str) -> None:
    sobre = _sobre(_evento(None))
    del sobre[falta]
    with pytest.raises(ValidationError):
        validador.validate(sobre)


def test_el_sobre_comun_es_el_de_los_eventos_del_consentimiento() -> None:
    """Mismo sobre (ADR 0009) salvo `aggregateVersion`, que no lleva."""
    revocado = json.loads(
        (EVENTOS / "ConsentimientoRevocado.v1.schema.json").read_text(encoding="utf-8")
    )
    verificacion = json.loads((EVENTOS / ARCHIVO).read_text(encoding="utf-8"))
    assert verificacion["required"] == revocado["required"]
    for campo in set(revocado["properties"]) - {"type", "aggregateVersion", "payload"}:
        assert verificacion["properties"][campo] == revocado["properties"][campo]
