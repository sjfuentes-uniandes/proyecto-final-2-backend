"""Manejador de `IndemnizacionConfirmada` e `IndemnizacionRechazada` v1 para el consumidor con inbox
(HU-W23; ADR 0015; DS3-25).

Cola `siniestros-indemnizaciones` (filtro de prefijo `Indemnizacion`, visibilidad de 30 s). Modo (a)
de `solventa_common.inbox`: todo en la transacción del inbox, sin continuación. Valida el cuerpo con
el modelo propio de `siniestros` (lector tolerante: solo los campos que usa; la prueba de contrato
`test_indemnizaciones_v1.py` los compara con los esquemas que publica `dinero`) y delega en
`RegistrarResultadoPago`.

Lanza `EventoInvalido` (rollback y sin borrar: a la DLQ tras 5 recepciones) si el cuerpo no valida.
El WARNING nombra los campos, nunca sus valores. Un tipo `Indemnizacion*` que `siniestros` no usa se
consume sin efecto: el filtro es de prefijo y un evento nuevo no debe llenar la DLQ.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.inbox import Continuacion, EventoInvalido, EventoRecibido

from siniestros.application.siniestro.puertos import TxSiniestros
from siniestros.application.siniestro.resultado_pago import RegistrarResultadoPago
from siniestros.domain.siniestro.pago import EstadoPago, ResultadoPago

MENSAJE_INVALIDO = "pago: Indemnizacion* inválido; va a la DLQ"
MENSAJE_IGNORADO = "pago: tipo de evento sin efecto en siniestros; se consume"

logger = logging.getLogger("siniestros.pago")

REFERENCIA_PAGO = r"^IND-88-[0-9]{4}-[0-9]{4,}-[0-9]{2}$"
CODIGO = r"^[A-Z][A-Z0-9_]{0,63}$"


class _Evento(BaseModel):
    # Lector tolerante: solo los campos que usa siniestros; el resto del evento se ignora.
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore"
    )


class IndemnizacionConfirmadaV1(_Evento):
    siniestro_id: UUID
    referencia: Annotated[str, Field(pattern=REFERENCIA_PAGO)]
    transaccion_id: Annotated[str, Field(min_length=1, max_length=128)]
    intentos: Annotated[int, Field(ge=1)]
    duplicados_descartados: Annotated[int, Field(ge=0)]
    confirmada_en: datetime


class IndemnizacionRechazadaV1(_Evento):
    siniestro_id: UUID
    referencia: Annotated[str, Field(pattern=REFERENCIA_PAGO)]
    motivo: Annotated[str, Field(pattern=CODIGO)]
    intentos: Annotated[int, Field(ge=1)]
    duplicados_descartados: Annotated[int, Field(ge=0)]
    rechazada_en: datetime


class _Sobre(_Evento):
    event_id: UUID
    aggregate_id: UUID
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str


class SobreConfirmadaV1(_Sobre):
    type: Literal["IndemnizacionConfirmada"]
    payload: IndemnizacionConfirmadaV1


class SobreRechazadaV1(_Sobre):
    type: Literal["IndemnizacionRechazada"]
    payload: IndemnizacionRechazadaV1


_SOBRE: TypeAdapter[SobreConfirmadaV1 | SobreRechazadaV1] = TypeAdapter(
    Annotated[SobreConfirmadaV1 | SobreRechazadaV1, Field(discriminator="type")]
)
_TIPOS = frozenset({"IndemnizacionConfirmada", "IndemnizacionRechazada"})


def _campos(error: ValidationError) -> list[str]:
    return sorted({".".join(str(parte) for parte in e["loc"]) for e in error.errors()})


def _invalido(evento: EventoRecibido, campos: list[str]) -> EventoInvalido:
    logger.warning(
        MENSAJE_INVALIDO,
        extra={"event_id": str(evento.event_id), "message_id": evento.message_id, "fields": campos},
    )
    return EventoInvalido()


def resultado_de(sobre: SobreConfirmadaV1 | SobreRechazadaV1) -> ResultadoPago:
    p = sobre.payload
    if isinstance(p, IndemnizacionConfirmadaV1):
        return ResultadoPago(
            siniestro_id=p.siniestro_id,
            estado=EstadoPago.CONFIRMADO,
            referencia=p.referencia,
            transaccion_id=p.transaccion_id,
            motivo=None,
            intentos=p.intentos,
            duplicados_descartados=p.duplicados_descartados,
            ocurrido_en=p.confirmada_en,
        )
    return ResultadoPago(
        siniestro_id=p.siniestro_id,
        estado=EstadoPago.RECHAZADO,
        referencia=p.referencia,
        transaccion_id=None,
        motivo=p.motivo,
        intentos=p.intentos,
        duplicados_descartados=p.duplicados_descartados,
        ocurrido_en=p.rechazada_en,
    )


def manejador_indemnizaciones[T](
    caso: RegistrarResultadoPago, abrir: Callable[[T], TxSiniestros]
) -> Callable[[EventoRecibido, T], Awaitable[Continuacion | None]]:
    """El manejador (modo a) para `ConsumidorInbox`. `abrir` arma la vista de `siniestros` sobre la
    transacción del inbox: así el resultado y la fila del inbox se confirman juntos."""

    async def manejar(evento: EventoRecibido, tx: T) -> Continuacion | None:
        if evento.event_type not in _TIPOS:
            logger.info(MENSAJE_IGNORADO, extra={"event_type": evento.event_type})
            return None
        try:
            sobre = _SOBRE.validate_json(evento.cuerpo)
        except ValidationError as error:
            raise _invalido(evento, _campos(error)) from None
        await caso.registrar(resultado_de(sobre), abrir(tx))
        return None

    return manejar


__all__ = [
    "IndemnizacionConfirmadaV1",
    "IndemnizacionRechazadaV1",
    "SobreConfirmadaV1",
    "SobreRechazadaV1",
    "manejador_indemnizaciones",
    "resultado_de",
]
