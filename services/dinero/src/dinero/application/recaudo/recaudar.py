"""Recaudar el primer cobro de la prima a partir de `OfertaAceptada` (HU-W16, 16.10; plan §d;
ADR 0013, decisiones 2, 4 y 5).

- `registrar` corre en la transacción del inbox: crea el movimiento PENDIENTE (índice único por
  `claveRecaudo`). Solo si lo creó esta entrega devuelve la continuación que cobra; el consumidor la
  ejecuta después del commit (modo b de `solventa_common.inbox`). Si el movimiento ya existía, no se
  cobra: si sigue PENDIENTE, lo resolverá la conciliación (16.11).
- `cobrar` llama al adaptador y, en otra transacción, aplica el resultado con la guarda de PENDIENTE
  y escribe el evento en el outbox, solo si cambió la fila y el estado es final. Un incierto deja
  el movimiento PENDIENTE con su código y sin evento: nunca se asume rechazo.
- No lanza por fallos del adaptador (el puerto los devuelve como `CobroIncierto`); solo si falla
  la base. Entonces el mensaje se reentrega, llega como duplicado y el movimiento queda PENDIENTE.
- Los logs llevan `claveRecaudo` y `recaudoId`, nunca el pagador ni cuerpos.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from uuid import UUID

from dinero.application.recaudo.puertos import (
    CobroAprobado,
    CobroRechazado,
    EventosRecaudo,
    PagosPuerto,
    RecaudosRepositorio,
    Reloj,
    ResultadoCobro,
    SolicitudCobro,
    Transacciones,
)
from dinero.domain.recaudo.errores import RecaudoInvalido
from dinero.domain.recaudo.modelos import Recaudo, motivo_de_rechazo

Continuacion = Callable[[], Awaitable[None]]

MENSAJE_YA_REGISTRADO = "recaudo: la claveRecaudo ya tiene movimiento; no se cobra"
MENSAJE_RESUELTO = "recaudo: cobro resuelto"
MENSAJE_INCIERTO = "recaudo: cobro incierto; queda PENDIENTE"
MENSAJE_YA_RESUELTO = "recaudo: otro proceso ya lo resolvió; sin evento"

logger = logging.getLogger("dinero.recaudo")


class OfertaInvalida(ValueError):
    """La oferta no cumple una invariante del movimiento. `campo` lo nombra, nunca su valor."""

    def __init__(self, mensaje: str) -> None:
        super().__init__(mensaje)
        self.campo = mensaje.split(":", 1)[0]


@dataclass(frozen=True, slots=True)
class OfertaAceptada:
    """Lo que dinero usa de `OfertaAceptada` v1. `pagador_ref` es el `clienteId` sintético."""

    clave_recaudo: UUID
    cotizacion_id: UUID
    pagador_ref: UUID
    monto: Decimal
    moneda: str
    periodicidad: str
    correlation_id: str


def _ids(recaudo: Recaudo) -> dict[str, str]:
    return {"clave_recaudo": str(recaudo.clave_recaudo), "recaudo_id": str(recaudo.recaudo_id)}


class RecaudarPrima[T]:
    def __init__(
        self,
        *,
        recaudos: RecaudosRepositorio[T],
        transacciones: Transacciones[T],
        eventos: EventosRecaudo[T],
        pagos: PagosPuerto,
        reloj: Reloj,
    ) -> None:
        self._recaudos = recaudos
        self._transacciones = transacciones
        self._eventos = eventos
        self._pagos = pagos
        self._reloj = reloj

    async def registrar(self, oferta: OfertaAceptada, tx: T) -> Continuacion | None:
        """Crea el movimiento PENDIENTE en `tx`; la continuación cobra solo si es nuevo."""
        try:
            pendiente = Recaudo.pendiente(
                clave_recaudo=oferta.clave_recaudo,
                cotizacion_id=oferta.cotizacion_id,
                pagador_ref=oferta.pagador_ref,
                monto=oferta.monto,
                moneda=oferta.moneda,
                periodicidad=oferta.periodicidad,
                correlation_id=oferta.correlation_id,
                ahora=self._reloj.ahora(),
            )
        except RecaudoInvalido as error:
            raise OfertaInvalida(str(error)) from None
        creacion = await self._recaudos.crear_pendiente(pendiente, tx)
        if not creacion.nuevo:
            existente = creacion.recaudo
            logger.info(
                MENSAJE_YA_REGISTRADO, extra={**_ids(existente), "estado": existente.estado.value}
            )
            return None
        return partial(self.cobrar, creacion.recaudo)

    async def cobrar(self, recaudo: Recaudo) -> None:
        """Cobra un movimiento PENDIENTE y registra el resultado con su evento, si es final."""
        resultado = await self._pagos.cobrar(
            SolicitudCobro(
                clave_recaudo=recaudo.clave_recaudo,
                monto=recaudo.monto,
                moneda=recaudo.moneda.value,
                pagador_ref=recaudo.pagador_ref,
                correlation_id=recaudo.correlation_id,
            )
        )
        resuelto = _aplicar(recaudo, resultado, self._reloj)
        async with self._transacciones.transaccion() as tx:
            cambiadas = await self._recaudos.registrar_resultado(resuelto, tx)
            if cambiadas and resuelto.final:
                await self._eventos.agregar(resuelto, tx)
        if not cambiadas:
            logger.info(MENSAJE_YA_RESUELTO, extra=_ids(resuelto))
        elif resuelto.final:
            logger.info(
                MENSAJE_RESUELTO,
                extra={
                    **_ids(resuelto),
                    "estado": resuelto.estado.value,
                    "motivo": resuelto.motivo,
                },
            )
        else:
            logger.warning(
                MENSAJE_INCIERTO,
                extra={
                    **_ids(resuelto),
                    "estado": resuelto.estado.value,
                    "codigo": resuelto.ultimo_incierto,
                },
            )


def _aplicar(recaudo: Recaudo, resultado: ResultadoCobro, reloj: Reloj) -> Recaudo:
    ahora = reloj.ahora()
    if isinstance(resultado, CobroAprobado):
        return recaudo.confirmar(resultado.transaccion_id, ahora)
    if isinstance(resultado, CobroRechazado):
        return recaudo.rechazar(
            motivo_de_rechazo(resultado.motivo), ahora, transaccion_id=resultado.transaccion_id
        )
    return recaudo.registrar_incierto(resultado.codigo, ahora)
