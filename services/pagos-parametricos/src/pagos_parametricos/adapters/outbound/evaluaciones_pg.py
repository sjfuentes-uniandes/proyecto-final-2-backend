"""Evaluaciones, activaciones y el Outbox del evento evaluado en PostgreSQL (HU-W21): tablas de
`migraciones/002_parametricos.sql` y `outbox` de `001_descartes.sql`.

`registrar` escribe todo en la transacción del inbox: `eventos_evaluados` (la decisión del evento y
su origen), una `evaluaciones` por póliza candidata, una `activaciones` `PENDIENTE` por cada póliza
que activó (la barrera de negocio `UNIQUE (evento_id, poliza_id)`; W22 las convierte en siniestros)
y `EventoParametricoEvaluado` en el `outbox`. El relay lo publica después del commit.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb
from solventa_common.inbox import TransaccionInboxPg

from pagos_parametricos.adapters.outbound.eventos import (
    EVENTO_PARAMETRICO_EVALUADO,
    EventoParametricoEvaluadoV1,
    RazonEvento,
    SobreEventoParametricoEvaluadoV1,
)
from pagos_parametricos.domain.parametrico.modelos import (
    VERSION_EVALUADOR,
    DecisionPoliza,
    EvaluacionEvento,
    EventoObservado,
)

_DUPLICADO = (
    "UPDATE eventos_evaluados SET duplicados = duplicados + 1 WHERE evento_id = %s "
    "RETURNING evento_id"
)
_EVENTO = (
    "INSERT INTO eventos_evaluados (evento_id, fuente, id_externo, evento_origen, decision, razon, "
    "evaluadas, activaciones, version_regla, evaluado_en) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_EVALUACION = (
    "INSERT INTO evaluaciones (evento_id, poliza_id, decision, razon, version_regla, evaluado_en) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
_ACTIVACION = (
    "INSERT INTO activaciones (evento_id, poliza_id, estado, valor_pago, moneda, version_regla, "
    "creada_en) VALUES (%s, %s, 'PENDIENTE', %s, %s, %s, %s)"
)
_OUTBOX = (
    "INSERT INTO outbox (event_id, aggregate_id, event_type, event_version, occurred_at, "
    "correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


def sobre_de(
    evento: EventoObservado,
    resultado: EvaluacionEvento,
    evaluado_en: datetime,
    correlation_id: str,
) -> SobreEventoParametricoEvaluadoV1:
    """El sobre del evento evaluado: `aggregateId` = `eventoId` y un `eventId` determinista, para
    que el Outbox no pueda duplicarlo."""
    return SobreEventoParametricoEvaluadoV1(
        event_id=uuid5(evento.evento_id, EVENTO_PARAMETRICO_EVALUADO),
        aggregate_id=evento.evento_id,
        type=EVENTO_PARAMETRICO_EVALUADO,
        version=1,
        occurred_at=evaluado_en,
        correlation_id=correlation_id,
        payload=EventoParametricoEvaluadoV1(
            evento_id=evento.evento_id,
            fuente=evento.fuente,
            id_externo=evento.id_externo,
            evento_origen=evento.evento_origen,
            decision=resultado.decision,
            razon=RazonEvento(resultado.razon.value) if resultado.razon else None,
            activaciones=resultado.activaciones,
            evaluadas=resultado.evaluadas,
            version_regla=VERSION_EVALUADOR,
            evaluado_en=evaluado_en,
        ),
    )


class EvaluacionesPg:
    """Implementa `RepositorioEvaluaciones[TransaccionInboxPg]`."""

    async def descartar_duplicado(self, tx: TransaccionInboxPg, evento_id: UUID) -> bool:
        cursor = await tx.conexion.execute(_DUPLICADO, (evento_id,))
        return await cursor.fetchone() is not None

    async def registrar(
        self,
        tx: TransaccionInboxPg,
        evento: EventoObservado,
        resultado: EvaluacionEvento,
        evaluado_en: datetime,
        correlation_id: str,
    ) -> None:
        conexion = tx.conexion
        await conexion.execute(
            _EVENTO,
            (
                evento.evento_id,
                evento.fuente,
                evento.id_externo,
                evento.evento_origen,
                resultado.decision.value,
                resultado.razon.value if resultado.razon else None,
                resultado.evaluadas,
                resultado.activaciones,
                VERSION_EVALUADOR,
                evaluado_en,
            ),
        )
        if resultado.evaluaciones:
            async with conexion.cursor() as cursor:
                await cursor.executemany(
                    _EVALUACION,
                    [
                        (
                            evento.evento_id,
                            e.poliza_id,
                            e.decision.value,
                            e.razon.value if e.razon else None,
                            e.version_regla,
                            evaluado_en,
                        )
                        for e in resultado.evaluaciones
                    ],
                )
                activas: list[tuple[Any, ...]] = [
                    (
                        evento.evento_id,
                        e.poliza_id,
                        e.valor_pago,
                        e.moneda,
                        e.version_regla,
                        evaluado_en,
                    )
                    for e in resultado.evaluaciones
                    if e.decision is DecisionPoliza.ACTIVA
                ]
                if activas:
                    await cursor.executemany(_ACTIVACION, activas)
        sobre = sobre_de(evento, resultado, evaluado_en, correlation_id)
        await conexion.execute(
            _OUTBOX,
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
