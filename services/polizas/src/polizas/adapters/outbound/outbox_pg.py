"""Transactional Outbox de polizas (regla 3): el evento se escribe en la misma transacción que la
decisión. La publicación a SNS la hace el relay (EXT-04); aquí solo se insertan filas.
"""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from polizas.adapters.outbound.eventos import (
    DECISION_SUSCRIPCION_REGISTRADA,
    DecisionSuscripcionRegistradaV1,
    SobreEvento,
)
from polizas.domain.suscripcion.modelos import Decision

_INSERTAR = (
    "INSERT INTO outbox (event_id, aggregate_id, event_type, event_version, occurred_at, "
    "correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


def evento_de(decision: Decision) -> SobreEvento[DecisionSuscripcionRegistradaV1]:
    """DecisionSuscripcionRegistrada.v1 de la decisión, sin montos ni insumos."""
    evaluacion = decision.evaluacion
    return SobreEvento[DecisionSuscripcionRegistradaV1](
        event_id=uuid4(),
        aggregate_id=decision.id,
        type=DECISION_SUSCRIPCION_REGISTRADA,
        version=1,
        occurred_at=decision.decidida_en,
        correlation_id=decision.correlation_id,
        payload=DecisionSuscripcionRegistradaV1(
            decision_id=decision.id,
            cotizacion_id=decision.cotizacion_id,
            oferta_id=decision.oferta_id,
            cliente_id=decision.cliente_id,
            producto_id=decision.producto_id,
            resultado=decision.resultado,
            tipo=decision.tipo,
            regla_determinante=evaluacion.regla_determinante,
            reglas_no_cumplidas=list(evaluacion.reglas_no_cumplidas),
            version_reglas=decision.version_reglas,
            hash_reglas=decision.hash_reglas,
            hash_insumos=decision.hash_insumos,
            actor_id=decision.actor_id,
            decidida_en=decision.decidida_en,
        ),
    )


async def insertar(conexion: AsyncConnection[Any], sobres: Sequence[SobreEvento[Any]]) -> None:
    """Inserta los sobres en la transacción abierta de `conexion` (el payload en su columna)."""
    for sobre in sobres:
        await conexion.execute(
            _INSERTAR,
            (
                sobre.event_id,
                sobre.aggregate_id,
                sobre.type,
                sobre.version,
                sobre.occurred_at,
                sobre.correlation_id,
                Jsonb(sobre.payload.model_dump(mode="json", by_alias=True)),
            ),
        )
