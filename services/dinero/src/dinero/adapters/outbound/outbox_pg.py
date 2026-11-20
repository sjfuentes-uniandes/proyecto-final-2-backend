"""Transactional Outbox de dinero (HU-W16, 16.10; HU-W23): el evento `Recaudo*` o `Indemnizacion*`
se escribe en la misma transacción que el resultado del movimiento. La publicación a SNS la hace el relay de
`solventa_common.outbox`; aquí solo se insertan filas (`published_at` solo lo marca el relay).
"""

from decimal import Decimal
from uuid import uuid4

from psycopg.types.json import Jsonb

from dinero.adapters.outbound.eventos import (
    INDEMNIZACION_CONFIRMADA,
    INDEMNIZACION_RECHAZADA,
    RECAUDO_CONFIRMADO,
    RECAUDO_RECHAZADO,
    IndemnizacionConfirmadaV1,
    IndemnizacionRechazadaV1,
    MontoV1,
    RecaudoConfirmadoV1,
    RecaudoRechazadoV1,
    SobreEvento,
)
from dinero.adapters.outbound.recaudos_pg import ConTransaccion
from dinero.domain.indemnizacion.modelos import EstadoIndemnizacion, Indemnizacion
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


def evento_de_indemnizacion(
    indemnizacion: Indemnizacion,
) -> SobreEvento[IndemnizacionConfirmadaV1] | SobreEvento[IndemnizacionRechazadaV1]:
    """El evento del estado final; `aggregateId` = `indemnizacionId` y `occurredAt` =
    `resuelto_en`. Sin la cuenta ni el `beneficiarioRef`."""
    if indemnizacion.resuelto_en is None or indemnizacion.estado is EstadoIndemnizacion.PENDIENTE:
        raise ValueError("evento_de_indemnizacion: una PENDIENTE no tiene evento")
    if indemnizacion.estado is EstadoIndemnizacion.CONFIRMADA:
        assert indemnizacion.transaccion_id is not None  # invariante del dominio
        return SobreEvento[IndemnizacionConfirmadaV1](
            event_id=uuid4(),
            aggregate_id=indemnizacion.indemnizacion_id,
            type=INDEMNIZACION_CONFIRMADA,
            version=1,
            occurred_at=indemnizacion.resuelto_en,
            correlation_id=indemnizacion.correlation_id,
            payload=IndemnizacionConfirmadaV1(
                indemnizacion_id=indemnizacion.indemnizacion_id,
                siniestro_id=indemnizacion.siniestro_id,
                referencia=indemnizacion.referencia,
                # Texto decimal exacto: sin float, sin notación científica y con 2 decimales.
                monto=format(indemnizacion.monto.quantize(Decimal("0.01")), "f"),
                moneda=indemnizacion.moneda.value,
                transaccion_id=indemnizacion.transaccion_id,
                intentos=indemnizacion.intentos,
                duplicados_descartados=indemnizacion.duplicados_descartados,
                confirmada_en=indemnizacion.resuelto_en,
            ),
        )
    assert indemnizacion.motivo is not None  # invariante del dominio
    return SobreEvento[IndemnizacionRechazadaV1](
        event_id=uuid4(),
        aggregate_id=indemnizacion.indemnizacion_id,
        type=INDEMNIZACION_RECHAZADA,
        version=1,
        occurred_at=indemnizacion.resuelto_en,
        correlation_id=indemnizacion.correlation_id,
        payload=IndemnizacionRechazadaV1(
            indemnizacion_id=indemnizacion.indemnizacion_id,
            siniestro_id=indemnizacion.siniestro_id,
            referencia=indemnizacion.referencia,
            motivo=indemnizacion.motivo,
            intentos=indemnizacion.intentos,
            duplicados_descartados=indemnizacion.duplicados_descartados,
            rechazada_en=indemnizacion.resuelto_en,
        ),
    )


class OutboxIndemnizacionesPg:
    """Implementa EventosIndemnizacion[ConTransaccion]: inserta en la transacción abierta."""

    async def agregar(self, indemnizacion: Indemnizacion, tx: ConTransaccion) -> None:
        sobre = evento_de_indemnizacion(indemnizacion)
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
