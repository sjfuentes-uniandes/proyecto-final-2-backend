"""Manejador de `RecaudoConfirmado` v1 y `RecaudoRechazado` v1 para el consumidor de
`polizas-recaudos` (HU-W16, 16.13; ADR 0013, decisión 9).

Modo (a) de `solventa_common.inbox`: la proyección se escribe en la transacción del inbox y no hay
continuación. Valida el cuerpo con modelos propios (lector tolerante: solo los campos que usa; la
prueba de contrato `test_recaudos_v1.py` los compara con los esquemas que publica dinero).

Lanza `EventoInvalido` (rollback y sin borrar: a la DLQ tras 5 recepciones) si el `type` no es
ninguno de los dos o si el cuerpo no valida. El WARNING nombra los campos, nunca sus valores. El
monto se lee del texto decimal con `Decimal`, sin pasar por `float`.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.inbox import Continuacion, EventoInvalido, EventoRecibido

from polizas.application.recaudo.proyectar import ProyectarRecaudo
from polizas.domain.recaudo.modelos import ResultadoRecaudo, SituacionRecaudo

RECAUDO_CONFIRMADO = "RecaudoConfirmado"
RECAUDO_RECHAZADO = "RecaudoRechazado"
MENSAJE_INVALIDO = "recaudo: evento de recaudo inválido; va a la DLQ"

logger = logging.getLogger("polizas.recaudo")


class _Evento(BaseModel):
    # Lector tolerante: solo los campos que usa polizas; el resto del evento se ignora.
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore"
    )


class MontoV1(_Evento):
    valor: Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]{1,2})?$")]
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class RecaudoConfirmadoV1(_Evento):
    recaudo_id: UUID
    clave_recaudo: UUID
    monto: MontoV1
    transaccion_id: Annotated[str, Field(min_length=1, max_length=128)]
    confirmado_en: AwareDatetime


class RecaudoRechazadoV1(_Evento):
    recaudo_id: UUID
    clave_recaudo: UUID
    motivo: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
    rechazado_en: AwareDatetime


class SobreRecaudoConfirmadoV1(_Evento):
    event_id: UUID
    aggregate_id: UUID
    type: Literal["RecaudoConfirmado"]
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: RecaudoConfirmadoV1


class SobreRecaudoRechazadoV1(_Evento):
    event_id: UUID
    aggregate_id: UUID
    type: Literal["RecaudoRechazado"]
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: RecaudoRechazadoV1


_SOBRES: dict[str, type[SobreRecaudoConfirmadoV1] | type[SobreRecaudoRechazadoV1]] = {
    RECAUDO_CONFIRMADO: SobreRecaudoConfirmadoV1,
    RECAUDO_RECHAZADO: SobreRecaudoRechazadoV1,
}


def resultado_de(sobre: SobreRecaudoConfirmadoV1 | SobreRecaudoRechazadoV1) -> ResultadoRecaudo:
    payload = sobre.payload
    if isinstance(payload, RecaudoConfirmadoV1):
        return ResultadoRecaudo(
            clave_recaudo=payload.clave_recaudo,
            recaudo_id=payload.recaudo_id,
            estado=SituacionRecaudo.CONFIRMADO,
            monto=Decimal(payload.monto.valor),
            moneda=payload.monto.moneda,
            transaccion_id=payload.transaccion_id,
            motivo=None,
            ocurrido_en=payload.confirmado_en,
        )
    return ResultadoRecaudo(
        clave_recaudo=payload.clave_recaudo,
        recaudo_id=payload.recaudo_id,
        estado=SituacionRecaudo.RECHAZADO,
        monto=None,
        moneda=None,
        transaccion_id=None,
        motivo=payload.motivo,
        ocurrido_en=payload.rechazado_en,
    )


def _campos(error: ValidationError) -> list[str]:
    return sorted({".".join(str(parte) for parte in e["loc"]) for e in error.errors()})


def _invalido(evento: EventoRecibido, campos: list[str]) -> EventoInvalido:
    logger.warning(
        MENSAJE_INVALIDO,
        extra={
            "event_id": str(evento.event_id),
            "event_type": evento.event_type,
            "message_id": evento.message_id,
            "fields": campos,
        },
    )
    return EventoInvalido()


def manejador_recaudos[T](
    caso: ProyectarRecaudo[T],
) -> Callable[[EventoRecibido, T], Awaitable[Continuacion | None]]:
    """El manejador (modo a) para `ConsumidorInbox`: proyecta y devuelve None."""

    async def manejar(evento: EventoRecibido, tx: T) -> Continuacion | None:
        modelo = _SOBRES.get(evento.event_type)
        if modelo is None:
            raise _invalido(evento, ["type"])
        try:
            sobre = modelo.model_validate_json(evento.cuerpo)
        except ValidationError as error:
            raise _invalido(evento, _campos(error)) from None
        # HU-W17 (17.8): el correlationId validado por el inbox (None si no tenía la forma).
        await caso.ejecutar(resultado_de(sobre), tx, evento.correlation_id)
        return None

    return manejar
