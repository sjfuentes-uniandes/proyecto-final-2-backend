"""Manejador de `OfertaAceptada` v1 para el consumidor con inbox (HU-W16, 16.10; P16-4).

Valida el cuerpo con el modelo propio de dinero (lector tolerante: solo los campos que usa; la
prueba de contrato `test_oferta_aceptada_v1.py` los compara con el esquema que publica polizas) y
delega en `RecaudarPrima.registrar`, en la transacción del inbox.

Lanza `EventoInvalido` (rollback y sin borrar: a la DLQ tras 5 recepciones) si el cuerpo no valida,
o si el movimiento no se puede crear: moneda distinta de COP, o `prima.valor` con más de 2 decimales
o no mayor que 0 (reglas del dominio). El WARNING nombra los campos, nunca sus valores.

El monto se lee del texto decimal con `Decimal`, sin pasar por `float`. Si el inbox descartó el
`correlationId` del sobre (sin forma válida), se genera uno para el movimiento.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.inbox import Continuacion, EventoInvalido, EventoRecibido

from dinero.application.recaudo.recaudar import OfertaAceptada, OfertaInvalida, RecaudarPrima

MENSAJE_INVALIDO = "recaudo: OfertaAceptada inválida; va a la DLQ"

logger = logging.getLogger("dinero.recaudo")


class _Evento(BaseModel):
    # Lector tolerante: solo los campos que usa dinero; el resto del evento se ignora.
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore"
    )


class PrimaV1(_Evento):
    valor: Annotated[str, Field(pattern=r"^\d+(\.\d+)?$")]
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    periodicidad: str


class OfertaAceptadaV1(_Evento):
    cotizacion_id: UUID
    cliente_id: UUID
    prima: PrimaV1
    clave_recaudo: UUID


class SobreOfertaAceptadaV1(_Evento):
    event_id: UUID
    aggregate_id: UUID
    type: Literal["OfertaAceptada"]
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: OfertaAceptadaV1


def _campos(error: ValidationError) -> list[str]:
    return sorted({".".join(str(parte) for parte in e["loc"]) for e in error.errors()})


def _invalido(evento: EventoRecibido, campos: list[str]) -> EventoInvalido:
    logger.warning(
        MENSAJE_INVALIDO,
        extra={"event_id": str(evento.event_id), "message_id": evento.message_id, "fields": campos},
    )
    return EventoInvalido()


def manejador_oferta_aceptada[T](
    caso: RecaudarPrima[T],
) -> Callable[[EventoRecibido, T], Awaitable[Continuacion | None]]:
    """El manejador (modo b) para `ConsumidorInbox`: devuelve la continuación del cobro, o None."""

    async def manejar(evento: EventoRecibido, tx: T) -> Continuacion | None:
        try:
            sobre = SobreOfertaAceptadaV1.model_validate_json(evento.cuerpo)
        except ValidationError as error:
            raise _invalido(evento, _campos(error)) from None
        payload = sobre.payload
        oferta = OfertaAceptada(
            clave_recaudo=payload.clave_recaudo,
            cotizacion_id=payload.cotizacion_id,
            pagador_ref=payload.cliente_id,
            monto=Decimal(payload.prima.valor),
            moneda=payload.prima.moneda,
            periodicidad=payload.prima.periodicidad,
            correlation_id=evento.correlation_id or str(uuid4()),
        )
        try:
            return await caso.registrar(oferta, tx)
        except OfertaInvalida as error:
            raise _invalido(evento, [error.campo]) from None

    return manejar
