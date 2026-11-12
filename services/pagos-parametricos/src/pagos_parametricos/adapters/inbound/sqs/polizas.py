"""Manejador de los eventos `Poliza*` para el consumidor de `parametricos-polizas` (HU-W21, DS3-22;
modo (a) de `solventa_common.inbox`).

Mantiene `coberturas_activas` a partir de:

- `PolizaEmitida` v1 **con** `coberturaParametrica` (opción A de DS3-20): crea o reemplaza la
  cobertura, con la ventana de la `vigencia` de la póliza (`ventana_de_vigencia`).
- `PolizaCancelada` v1: forma **sintética** (`polizas` no la publica todavía; guía §5.1) que deja
  la cobertura cancelada.

Todo lo demás (una `PolizaEmitida` sin cobertura paramétrica, cualquier otro `Poliza*`) queda en el
inbox y se ignora sin error. El orden es `aggregateVersion` si el sobre lo trae y, si no,
`occurredAt` (DS3-22): un evento atrasado se confirma y no cambia nada. **No usa
`versionado.leer_version`** (lanza `EventoInvalido` sin `aggregateVersion` y mandaría todas las
pólizas a la DLQ). Una `version` distinta de 1 o un cuerpo que no valida lanza `EventoInvalido`; el
WARNING nombra los campos, nunca sus valores.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import date
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
from solventa_common.telemetry import operacion

from pagos_parametricos.adapters.inbound.sqs._comun import (
    Lector,
    campos_invalidos,
    leer_aggregate_version,
)
from pagos_parametricos.application.parametrico.aplicar_poliza import AplicarEventoPoliza
from pagos_parametricos.domain.parametrico.modelos import UNIDAD_POR_TIPO, Regla
from pagos_parametricos.domain.parametrico.orden import (
    EventoPoliza,
    PolizaCancelada,
    PolizaEmitida,
)
from pagos_parametricos.domain.parametrico.ventana import ventana_de_vigencia

POLIZA_EMITIDA = "PolizaEmitida"
POLIZA_CANCELADA = "PolizaCancelada"
OPERACION = "parametrico.coberturas"
IGNORADA = "ignorada"
MENSAJE_INVALIDO = "poliza: evento inválido; va a la DLQ"
MENSAJE_VERSION_NO_SOPORTADA = "poliza: versión del evento no soportada; va a la DLQ"
MENSAJE_IGNORADO = "poliza: evento ignorado"

UMBRAL = r"^[0-9]{1,9}(\.[0-9]{1,3})?$"
TEXTO_DECIMAL = r"^[0-9]+(\.[0-9]{1,2})?$"

logger = logging.getLogger("pagos_parametricos.consumo")


class CoberturaParametricaV1(Lector):
    tipo_evento: Literal["LLUVIA_ACUMULADA", "RETRASO_VUELO"]
    zona: Annotated[str, Field(pattern=r"^[A-Z0-9]{2,16}$")]
    umbral: Annotated[str, Field(pattern=UMBRAL)]
    unidad: Literal["mm", "min"]
    valor_pago: Annotated[str, Field(pattern=TEXTO_DECIMAL)]
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    version_regla: Annotated[str, Field(min_length=1, max_length=32)]


class VigenciaV1(Lector):
    desde: date
    hasta: date


class PolizaEmitidaV1(Lector):
    poliza_id: UUID
    numero: Annotated[str, Field(pattern=r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$")]
    vigencia: VigenciaV1
    cobertura_parametrica: CoberturaParametricaV1 | None = None


class SobrePolizaEmitidaV1(Lector):
    type: Literal["PolizaEmitida"]
    version: Literal[1]
    occurred_at: AwareDatetime
    payload: PolizaEmitidaV1


class PolizaCanceladaV1(Lector):
    poliza_id: UUID


class SobrePolizaCanceladaV1(Lector):
    type: Literal["PolizaCancelada"]
    version: Literal[1]
    occurred_at: AwareDatetime
    payload: PolizaCanceladaV1


def _regla_de(poliza: PolizaEmitidaV1, cobertura: CoberturaParametricaV1) -> Regla:
    if UNIDAD_POR_TIPO[cobertura.tipo_evento] != cobertura.unidad:
        raise EventoInvalido
    desde, hasta = poliza.vigencia.desde, poliza.vigencia.hasta
    if hasta < desde:
        raise EventoInvalido
    umbral, valor_pago = Decimal(cobertura.umbral), Decimal(cobertura.valor_pago)
    if umbral <= 0 or valor_pago <= 0:
        raise EventoInvalido
    inicio, fin = ventana_de_vigencia(desde, hasta)
    return Regla(
        tipo_evento=cobertura.tipo_evento,
        zona=cobertura.zona,
        umbral=umbral,
        unidad=cobertura.unidad,
        valor_pago=valor_pago,
        moneda=cobertura.moneda,
        version_regla=cobertura.version_regla,
        vigente_desde=inicio,
        vigente_hasta=fin,
    )


def manejador_polizas(
    caso: AplicarEventoPoliza[TransaccionInboxPg],
) -> Callable[[EventoRecibido, TransaccionInboxPg], Awaitable[Continuacion | None]]:
    """El manejador (modo a) para `ConsumidorInbox`: aplica y devuelve None."""

    def invalido(evento: EventoRecibido, campos: list[str]) -> EventoInvalido:
        logger.warning(
            MENSAJE_INVALIDO,
            extra={
                "event_id": str(evento.event_id),
                "message_id": evento.message_id,
                "fields": campos,
            },
        )
        return EventoInvalido()

    def ignorar(evento: EventoRecibido) -> None:
        with operacion(OPERACION) as op:
            op.marcar(IGNORADA)
            op.anotar(event_type=evento.event_type)
        logger.debug(
            MENSAJE_IGNORADO,
            extra={"event_id": str(evento.event_id), "event_type": evento.event_type},
        )

    def a_dominio(evento: EventoRecibido) -> EventoPoliza | None:
        """El evento de póliza que mueve la copia, o None si se ignora."""
        if evento.event_type not in (POLIZA_EMITIDA, POLIZA_CANCELADA):
            return None
        if evento.sobre.get("version") != 1:
            logger.warning(
                MENSAJE_VERSION_NO_SOPORTADA,
                extra={"event_id": str(evento.event_id), "message_id": evento.message_id},
            )
            raise EventoInvalido
        version = leer_aggregate_version(evento.sobre)
        try:
            if evento.event_type == POLIZA_CANCELADA:
                cancelada = SobrePolizaCanceladaV1.model_validate_json(evento.cuerpo)
                return PolizaCancelada(cancelada.payload.poliza_id, cancelada.occurred_at, version)
            emitida = SobrePolizaEmitidaV1.model_validate_json(evento.cuerpo)
        except ValidationError as error:
            raise invalido(evento, campos_invalidos(error)) from None
        p = emitida.payload
        if p.cobertura_parametrica is None:
            return None
        try:
            regla = _regla_de(p, p.cobertura_parametrica)
        except EventoInvalido:
            raise invalido(evento, ["coberturaParametrica"]) from None
        return PolizaEmitida(p.poliza_id, p.numero, regla, emitida.occurred_at, version)

    async def manejar(evento: EventoRecibido, tx: TransaccionInboxPg) -> Continuacion | None:
        aplicable = a_dominio(evento)
        if aplicable is None:
            ignorar(evento)
            return None
        await caso.ejecutar(aplicable, tx)
        return None

    return manejar
