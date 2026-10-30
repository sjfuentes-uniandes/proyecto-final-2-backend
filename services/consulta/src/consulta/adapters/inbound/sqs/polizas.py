"""Manejador de `PolizaEmitida` v1 para el consumidor de `consulta-proyeccion` (HU-M10).

Modo (a) de `solventa_common.inbox`: la proyección se escribe en la transacción del inbox y no hay
continuación. Lector tolerante: solo los campos que usa; la prueba de contrato compara los modelos
con `contracts/events/PolizaEmitida.v1.schema.json`.

La cola entrega los prefijos `Poliza` y `Siniestro`: cualquier tipo distinto de `PolizaEmitida` se
ignora sin error (queda en el inbox, se borra y se registra a nivel DEBUG). `PolizaEmitida` con una
`version` distinta de 1 no se ignora: lanza `EventoInvalido` (rollback y sin borrar: a la DLQ tras
5 recepciones) para no perder una versión que este consumidor aún no entiende. Un cuerpo que no
valida hace lo mismo. El WARNING nombra los campos, nunca sus valores.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.inbox import Continuacion, EventoInvalido, EventoRecibido

from consulta.application.proyeccion.comandos import PolizaEmitida
from consulta.application.proyeccion.proyectar_poliza import ProyectarPoliza

POLIZA_EMITIDA = "PolizaEmitida"
MENSAJE_INVALIDO = "poliza: evento PolizaEmitida inválido; va a la DLQ"
MENSAJE_IGNORADO = "proyeccion: evento ignorado"
MENSAJE_VERSION_NO_SOPORTADA = "poliza: versión de PolizaEmitida no soportada; va a la DLQ"

_TEXTO_DECIMAL = r"^[0-9]+(\.[0-9]{1,2})?$"

logger = logging.getLogger("consulta.proyeccion")


class _Evento(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore"
    )


class ProductoV1(_Evento):
    id: Annotated[str, Field(min_length=1)]
    version: Annotated[str, Field(min_length=1)]


class VigenciaV1(_Evento):
    desde: date
    hasta: date


class DocumentoV1(_Evento):
    huella: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class FirmaV1(_Evento):
    firma_id: Annotated[str, Field(min_length=1)]


class PolizaEmitidaV1(_Evento):
    poliza_id: UUID
    numero: Annotated[str, Field(pattern=r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$")]
    cliente_id: UUID
    producto: ProductoV1
    vigencia: VigenciaV1
    prima: Annotated[str, Field(pattern=_TEXTO_DECIMAL)]
    suma_asegurada: Annotated[str, Field(pattern=_TEXTO_DECIMAL)]
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    periodicidad: Annotated[str, Field(min_length=1)]
    documento: DocumentoV1
    firma: FirmaV1
    emitida_en: AwareDatetime


class SobrePolizaEmitidaV1(_Evento):
    type: Literal["PolizaEmitida"]
    version: Literal[1]
    payload: PolizaEmitidaV1


def evento_de(sobre: SobrePolizaEmitidaV1) -> PolizaEmitida:
    p = sobre.payload
    return PolizaEmitida(
        poliza_id=p.poliza_id,
        cliente_id=p.cliente_id,
        numero=p.numero,
        producto_id=p.producto.id,
        producto_version=p.producto.version,
        vigencia_desde=p.vigencia.desde,
        vigencia_hasta=p.vigencia.hasta,
        prima=p.prima,
        suma_asegurada=p.suma_asegurada,
        moneda=p.moneda,
        periodicidad=p.periodicidad,
        documento_huella=p.documento.huella,
        firma_id=p.firma.firma_id,
        emitida_en=p.emitida_en,
    )


def _campos(error: ValidationError) -> list[str]:
    return sorted({".".join(str(parte) for parte in e["loc"]) for e in error.errors()})


def manejador_polizas[T](
    caso: ProyectarPoliza[T],
) -> Callable[[EventoRecibido, T], Awaitable[Continuacion | None]]:
    """El manejador (modo a) para `ConsumidorInbox`: proyecta y devuelve None."""

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
        await caso.ejecutar(evento_de(sobre), tx)
        return None

    return manejar
