"""Transactional Outbox de `clientes`: el evento se escribe en la transacción del dato.

La publicación la hará el relay compartido de `solventa_common` (PR 2.2); aquí solo se insertan
filas con `published_at` nulo.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from clientes.adapters.outbound.eventos import (
    CONSENTIMIENTO_OTORGADO,
    CONSENTIMIENTO_REVOCADO,
    VERIFICACION_REGISTRADA,
)
from clientes.domain.consentimientos.modelos import Consentimiento
from clientes.domain.consentimientos.verificacion import Verificacion

_INSERTAR = (
    "INSERT INTO outbox (event_id, aggregate_id, event_type, event_version, occurred_at, "
    "correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


@dataclass(frozen=True, slots=True)
class EventoOutbox:
    event_id: UUID
    aggregate_id: UUID
    event_type: str
    event_version: int
    occurred_at: datetime
    correlation_id: str
    payload: dict[str, Any]


def _iso(instante: datetime) -> str:
    return instante.astimezone(UTC).isoformat()


def consentimiento_otorgado(
    consentimiento: Consentimiento, actor_id: str, correlation_id: str
) -> EventoOutbox:
    """Solo identificadores y códigos: sin datos personales (contrato v1 del plan)."""
    return EventoOutbox(
        event_id=uuid4(),
        aggregate_id=consentimiento.id,
        event_type=CONSENTIMIENTO_OTORGADO,
        event_version=1,
        occurred_at=consentimiento.otorgado_en,
        correlation_id=correlation_id,
        payload={
            "consentimientoId": str(consentimiento.id),
            "clienteId": str(consentimiento.cliente_id),
            "proposito": consentimiento.proposito,
            "alcances": list(consentimiento.alcances),
            "versionTexto": consentimiento.version_texto,
            "otorgadoEn": _iso(consentimiento.otorgado_en),
            "vigenteHasta": _iso(consentimiento.vigente_hasta),
            "actorId": actor_id,
        },
    )


def consentimiento_revocado(
    consentimiento: Consentimiento, actor_id: str, correlation_id: str
) -> EventoOutbox:
    """`occurredAt` es `revocado_en`, la captura del reloj de la sentencia que revocó (ADR 0010)."""
    revocado_en = consentimiento.revocado_en
    assert revocado_en is not None  # solo se llama con la fila que devolvió la revocación
    return EventoOutbox(
        event_id=uuid4(),
        aggregate_id=consentimiento.id,
        event_type=CONSENTIMIENTO_REVOCADO,
        event_version=1,
        occurred_at=revocado_en,
        correlation_id=correlation_id,
        payload={
            "consentimientoId": str(consentimiento.id),
            "clienteId": str(consentimiento.cliente_id),
            "revocadoEn": _iso(revocado_en),
            "actorId": actor_id,
        },
    )


def verificacion_registrada(verificacion: Verificacion, correlation_id: str) -> EventoOutbox:
    """Cada verificación es un evento propio (ADR 0011, P3-2): no se deduplica por decisión.
    `aggregateId` es el `clienteId` (ADR 0009) y `occurredAt` el `ahora` de la lectura."""
    evaluado = verificacion.consentimiento_evaluado
    return EventoOutbox(
        event_id=uuid4(),
        aggregate_id=verificacion.cliente_id,
        event_type=VERIFICACION_REGISTRADA,
        event_version=1,
        occurred_at=verificacion.verificado_en,
        correlation_id=correlation_id,
        payload={
            "clienteId": str(verificacion.cliente_id),
            "consentimientoId": str(evaluado) if evaluado is not None else None,
            "proposito": verificacion.proposito,
            "alcancesSolicitados": list(verificacion.alcances_solicitados),
            "decision": str(verificacion.decision),
            "causa": str(verificacion.causa) if verificacion.causa is not None else None,
            "alcancesRechazados": list(verificacion.alcances_rechazados),
            "verificadoEn": _iso(verificacion.verificado_en),
        },
    )


async def insertar(conexion: AsyncConnection[Any], evento: EventoOutbox) -> None:
    """Inserta el evento en la transacción abierta de `conexion`."""
    await conexion.execute(
        _INSERTAR,
        (
            evento.event_id,
            evento.aggregate_id,
            evento.event_type,
            evento.event_version,
            evento.occurred_at,
            evento.correlation_id,
            Jsonb(evento.payload),
        ),
    )
