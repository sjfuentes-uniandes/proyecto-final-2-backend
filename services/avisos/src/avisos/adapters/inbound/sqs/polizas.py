"""Manejador de `PolizaEmitida` v1 para el consumidor de la cola `avisos` (HU-M11).

Modo (b) de `solventa_common.inbox`: en la transacción del inbox se crean las entregas PENDIENTES (una
por dispositivo activo del cliente) y, después del commit, la continuación hace el primer intento de
envío. Si falla, no se reentrega el mensaje (el inbox lo trataría como duplicado): los reintentos son
del barrido.

La cola entrega `PolizaEmitida` y `RecaudoConfirmado`; cualquier tipo distinto de `PolizaEmitida` se
ignora sin error (queda en el inbox, se borra y se registra a nivel DEBUG), lo que deja el despacho
por `eventType` listo para otros tipos. `PolizaEmitida` con `version` distinta de 1, o un cuerpo que
no valida, lanza `EventoInvalido` (rollback y sin borrar: a la DLQ tras 5 recepciones). Lector
tolerante: solo `polizaId` y `clienteId`; el WARNING nombra campos, nunca valores.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.inbox import Continuacion, EventoInvalido, EventoRecibido

from avisos.application.entregas.enviar import EnviarEntregas
from avisos.application.entregas.registrar import RegistrarEntregas

POLIZA_EMITIDA = "PolizaEmitida"
MENSAJE_INVALIDO = "aviso: evento PolizaEmitida inválido; va a la DLQ"
MENSAJE_IGNORADO = "avisos: evento ignorado"
MENSAJE_VERSION_NO_SOPORTADA = "aviso: versión de PolizaEmitida no soportada; va a la DLQ"

logger = logging.getLogger("avisos.consumidor")


class _Evento(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore"
    )


class PolizaEmitidaV1(_Evento):
    poliza_id: UUID
    cliente_id: UUID


class SobrePolizaEmitidaV1(_Evento):
    type: Literal["PolizaEmitida"]
    version: Literal[1]
    event_id: UUID
    payload: PolizaEmitidaV1


def _campos(error: ValidationError) -> list[str]:
    return sorted({".".join(str(parte) for parte in e["loc"]) for e in error.errors()})


def manejador_polizas[T](
    registrar: RegistrarEntregas[T], enviar: EnviarEntregas[T]
) -> Callable[[EventoRecibido, T], Awaitable[Continuacion | None]]:
    """El manejador (modo b) para `ConsumidorInbox`."""

    async def manejar(evento: EventoRecibido, tx: T) -> Continuacion | None:
        if evento.event_type != POLIZA_EMITIDA:
            logger.debug(
                MENSAJE_IGNORADO,
                extra={"event_id": str(evento.event_id), "event_type": evento.event_type},
            )
            return None
        if evento.sobre.get("version") != 1:
            logger.warning(
                MENSAJE_VERSION_NO_SOPORTADA,
                extra={"event_id": str(evento.event_id), "message_id": evento.message_id},
            )
            raise EventoInvalido
        try:
            sobre = SobrePolizaEmitidaV1.model_validate_json(evento.cuerpo)
        except ValidationError as error:
            logger.warning(
                MENSAJE_INVALIDO,
                extra={
                    "event_id": str(evento.event_id),
                    "message_id": evento.message_id,
                    "fields": _campos(error),
                },
            )
            raise EventoInvalido from None
        creadas = await registrar.poliza_emitida(
            sobre.event_id, sobre.payload.poliza_id, sobre.payload.cliente_id, tx
        )
        if creadas == 0:
            return None

        async def primer_intento() -> None:
            await enviar.ejecutar(sobre.event_id)

        return primer_intento

    return manejar
