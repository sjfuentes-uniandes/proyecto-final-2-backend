"""Manejador de `SiniestroAprobado` v1 para el consumidor con inbox (HU-W23; ADR 0015, decisión 3).

Cola `dinero-indemnizaciones` (filtro `eventType = SiniestroAprobado`, visibilidad de 60 s). Valida
el cuerpo con el modelo propio de dinero (lector tolerante: solo los campos que usa; la prueba de
contrato `test_siniestro_aprobado_v1.py` los compara con el esquema que publica `siniestros`) y
delega en `PagarIndemnizacion.registrar`, en la transacción del inbox (modo b).

Lanza `EventoInvalido` (rollback y sin borrar: a la DLQ tras 5 recepciones) si el cuerpo no valida,
o si la indemnización no se puede crear: moneda distinta de COP, o `valorIndemnizar` con más de 2
decimales o no mayor que 0 (reglas del dominio). El WARNING nombra los campos, nunca sus valores:
ni el `beneficiarioRef`.

El monto se lee del texto decimal con `Decimal`, sin pasar por `float`. Si el inbox descartó el
`correlationId` del sobre (sin forma válida), se genera uno para la indemnización.
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

from dinero.application.indemnizacion.pagar import (
    AprobacionInvalida,
    PagarIndemnizacion,
    SiniestroAprobado,
)

MENSAJE_INVALIDO = "indemnizacion: SiniestroAprobado inválido; va a la DLQ"

logger = logging.getLogger("dinero.indemnizacion")


class _Evento(BaseModel):
    # Lector tolerante: solo los campos que usa dinero; el resto del evento se ignora.
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore"
    )


class SiniestroAprobadoV1(_Evento):
    siniestro_id: UUID
    valor_indemnizar: Annotated[str, Field(pattern=r"^\d+(\.\d+)?$")]
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    referencia_pago: Annotated[str, Field(pattern=r"^IND-88-[0-9]{4}-[0-9]{4,}-01$")]
    # Referencia opaca del beneficiario: nunca la cuenta (DS3-24).
    beneficiario_ref: Annotated[str, Field(min_length=1, max_length=128)]


class SobreSiniestroAprobadoV1(_Evento):
    event_id: UUID
    aggregate_id: UUID
    type: Literal["SiniestroAprobado"]
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: SiniestroAprobadoV1


def _campos(error: ValidationError) -> list[str]:
    return sorted({".".join(str(parte) for parte in e["loc"]) for e in error.errors()})


def _invalido(evento: EventoRecibido, campos: list[str]) -> EventoInvalido:
    logger.warning(
        MENSAJE_INVALIDO,
        extra={"event_id": str(evento.event_id), "message_id": evento.message_id, "fields": campos},
    )
    return EventoInvalido()


def manejador_siniestro_aprobado[T](
    caso: PagarIndemnizacion[T],
) -> Callable[[EventoRecibido, T], Awaitable[Continuacion | None]]:
    """El manejador (modo b) para `ConsumidorInbox`: devuelve la continuación del pago, o None."""

    async def manejar(evento: EventoRecibido, tx: T) -> Continuacion | None:
        try:
            sobre = SobreSiniestroAprobadoV1.model_validate_json(evento.cuerpo)
        except ValidationError as error:
            raise _invalido(evento, _campos(error)) from None
        payload = sobre.payload
        aprobado = SiniestroAprobado(
            siniestro_id=payload.siniestro_id,
            referencia=payload.referencia_pago,
            monto=Decimal(payload.valor_indemnizar),
            moneda=payload.moneda,
            beneficiario_ref=payload.beneficiario_ref,
            correlation_id=evento.correlation_id or str(uuid4()),
        )
        try:
            return await caso.registrar(aprobado, tx)
        except AprobacionInvalida as error:
            raise _invalido(evento, [error.campo]) from None

    return manejar
