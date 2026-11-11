"""Manejador de `EventoParametricoObservado` v1 para el consumidor de `parametricos-eventos`
(HU-W21, modo (a) de `solventa_common.inbox`).

La evaluación se escribe en la transacción del inbox y no hay continuación: **no se llama a
`siniestros`** (eso es HU-W22, que parte de las activaciones `PENDIENTE`). Lector tolerante: solo los
campos que usa; la prueba de contrato compara el modelo con
`contracts/events/EventoParametricoObservado.v1.schema.json`.

- Un tipo distinto de `EventoParametricoObservado` se ignora sin error (queda en el inbox).
- Una `version` distinta de 1, o un cuerpo que no valida (campos faltantes, formatos, unidad
  incoherente con el tipo, `eventId` distinto del `eventoId`), lanza `EventoInvalido`: rollback, sin
  borrar, y a la DLQ tras 5 recepciones con causa `FORMATO_INVALIDO` (H10). El WARNING nombra los
  campos, nunca sus valores.
- Opcional: `polizaId` en el payload (la fuente no lo envía) dirige la evaluación a esa póliza.
"""

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, ValidationError
from solventa_common.inbox import (
    Continuacion,
    EventoInvalido,
    EventoRecibido,
    TransaccionInboxPg,
)

from pagos_parametricos.adapters.inbound.sqs._comun import Lector, campos_invalidos
from pagos_parametricos.application.parametrico.evaluar_evento import EvaluarEvento
from pagos_parametricos.domain.parametrico.modelos import UNIDAD_POR_TIPO, EventoObservado

EVENTO_PARAMETRICO_OBSERVADO = "EventoParametricoObservado"
MENSAJE_INVALIDO = "parametrico: evento observado inválido; va a la DLQ"
MENSAJE_VERSION_NO_SOPORTADA = "parametrico: versión del evento no soportada; va a la DLQ"
MENSAJE_IGNORADO = "parametrico: evento ignorado"

FUENTE = r"^[A-Z][A-Z0-9-]{1,31}$"
IDENTIFICADOR = r"^[A-Za-z0-9._:-]{1,64}$"
ZONA = r"^[A-Z0-9]{2,16}$"
VALOR_OBSERVADO = r"^[0-9]{1,9}(\.[0-9]{1,3})?$"

logger = logging.getLogger("pagos_parametricos.consumo")


class EventoObservadoV1(Lector):
    evento_id: UUID
    fuente: Annotated[str, Field(pattern=FUENTE)]
    id_externo: Annotated[str, Field(pattern=IDENTIFICADOR)]
    evento_origen: Annotated[str, Field(pattern=IDENTIFICADOR)]
    tipo_evento: Literal["LLUVIA_ACUMULADA", "RETRASO_VUELO"]
    valor_observado: Annotated[str, Field(pattern=VALOR_OBSERVADO)]
    unidad: Literal["mm", "min"]
    zona: Annotated[str, Field(pattern=ZONA)]
    observado_en: AwareDatetime
    recibido_en: AwareDatetime
    archivo_ref: Annotated[str, Field(min_length=1)]
    # No lo envía la fuente: solo un reproceso dirigido a una póliza concreta.
    poliza_id: UUID | None = None


class SobreEventoObservadoV1(Lector):
    event_id: UUID
    type: Literal["EventoParametricoObservado"]
    version: Literal[1]
    payload: EventoObservadoV1


def evento_de(sobre: SobreEventoObservadoV1) -> EventoObservado:
    p = sobre.payload
    return EventoObservado(
        evento_id=p.evento_id,
        fuente=p.fuente,
        id_externo=p.id_externo,
        evento_origen=p.evento_origen,
        tipo_evento=p.tipo_evento,
        valor_observado=Decimal(p.valor_observado),
        unidad=p.unidad,
        zona=p.zona,
        observado_en=p.observado_en,
    )


def manejador_eventos(
    caso: EvaluarEvento[TransaccionInboxPg],
) -> Callable[[EventoRecibido, TransaccionInboxPg], Awaitable[Continuacion | None]]:
    """El manejador (modo a) para `ConsumidorInbox`: evalúa y devuelve None."""

    async def manejar(evento: EventoRecibido, tx: TransaccionInboxPg) -> Continuacion | None:
        if evento.event_type != EVENTO_PARAMETRICO_OBSERVADO:
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
            sobre = SobreEventoObservadoV1.model_validate_json(evento.cuerpo)
        except ValidationError as error:
            logger.warning(
                MENSAJE_INVALIDO,
                extra={
                    "event_id": str(evento.event_id),
                    "message_id": evento.message_id,
                    "fields": campos_invalidos(error),
                },
            )
            raise EventoInvalido from None
        p = sobre.payload
        # El eventId del sobre es el eventoId (DS3-16), y cada tipo se mide en una sola unidad.
        if sobre.event_id != p.evento_id or UNIDAD_POR_TIPO[p.tipo_evento] != p.unidad:
            logger.warning(
                MENSAJE_INVALIDO,
                extra={
                    "event_id": str(evento.event_id),
                    "message_id": evento.message_id,
                    "fields": ["eventoId" if sobre.event_id != p.evento_id else "unidad"],
                },
            )
            raise EventoInvalido
        await caso.ejecutar(evento_de(sobre), evento.correlation_id, tx, p.poliza_id)
        return None

    return manejar
