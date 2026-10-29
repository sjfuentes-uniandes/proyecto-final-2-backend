"""Transactional Outbox de dinero (HU-W16, 16.10): el evento `Recaudo*` se escribe en la misma
transacción que el resultado del movimiento. La publicación a SNS la hace el relay de
`solventa_common.outbox`; aquí solo se insertan filas (`published_at` solo lo marca el relay).
"""

from uuid import uuid4

from psycopg.types.json import Jsonb

from dinero.adapters.outbound.eventos import (
    RECAUDO_CONFIRMADO,
    RECAUDO_RECHAZADO,
    MontoV1,
    RecaudoConfirmadoV1,
    RecaudoRechazadoV1,
    SobreEvento,
)
from dinero.adapters.outbound.recaudos_pg import ConTransaccion
from dinero.domain.recaudo.modelos import EstadoRecaudo, Recaudo

_INSERTAR = (
    "INSERT INTO outbox (event_id, aggregate_id, event_type, event_version, occurred_at, "
    "correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


def evento_de(
    recaudo: Recaudo,
) -> SobreEvento[RecaudoConfirmadoV1] | SobreEvento[RecaudoRechazadoV1]:
    """El evento del estado final; `aggregateId` = `recaudoId` y `occurredAt` = `resuelto_en`."""
    if recaudo.resuelto_en is None or recaudo.estado is EstadoRecaudo.PENDIENTE:
        raise ValueError("evento_de: un movimiento PENDIENTE no tiene evento")
    if recaudo.estado is EstadoRecaudo.CONFIRMADO:
        assert recaudo.transaccion_id is not None  # invariante del dominio
        return SobreEvento[RecaudoConfirmadoV1](
            event_id=uuid4(),
            aggregate_id=recaudo.recaudo_id,
            type=RECAUDO_CONFIRMADO,
            version=1,
            occurred_at=recaudo.resuelto_en,
            correlation_id=recaudo.correlation_id,
            payload=RecaudoConfirmadoV1(
                recaudo_id=recaudo.recaudo_id,
                clave_recaudo=recaudo.clave_recaudo,
                cotizacion_id=recaudo.cotizacion_id,
                # Texto decimal exacto: sin float ni notación científica.
                monto=MontoV1(valor=format(recaudo.monto, "f"), moneda=recaudo.moneda.value),
                transaccion_id=recaudo.transaccion_id,
                confirmado_en=recaudo.resuelto_en,
            ),
        )
    assert recaudo.motivo is not None  # invariante del dominio
    return SobreEvento[RecaudoRechazadoV1](
        event_id=uuid4(),
        aggregate_id=recaudo.recaudo_id,
        type=RECAUDO_RECHAZADO,
        version=1,
        occurred_at=recaudo.resuelto_en,
        correlation_id=recaudo.correlation_id,
        payload=RecaudoRechazadoV1(
            recaudo_id=recaudo.recaudo_id,
            clave_recaudo=recaudo.clave_recaudo,
            cotizacion_id=recaudo.cotizacion_id,
            motivo=recaudo.motivo,
            rechazado_en=recaudo.resuelto_en,
        ),
    )


class OutboxRecaudosPg:
    """Implementa EventosRecaudo[ConTransaccion]: inserta en la transacción abierta de `tx`."""

    async def agregar(self, recaudo: Recaudo, tx: ConTransaccion) -> None:
        sobre = evento_de(recaudo)
        await tx.conexion.execute(
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
