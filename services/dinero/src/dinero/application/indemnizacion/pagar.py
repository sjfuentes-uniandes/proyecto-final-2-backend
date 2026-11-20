"""Pagar la indemnización a partir de `SiniestroAprobado` (HU-W23; ADR 0015, decisiones 3 y 4).

- `registrar` corre en la transacción del inbox: inserta la indemnización PENDIENTE con
  `ON CONFLICT (siniestro_id, tipo) DO UPDATE SET duplicados_descartados + 1`. Solo si la creó esta
  entrega devuelve la continuación que paga; el consumidor la ejecuta después del commit (modo b de
  `solventa_common.inbox`). Si ya existía (otro `eventId` del mismo siniestro, o dos consumidores a
  la vez), el duplicado ya quedó contado y no se llama a la pasarela.
- `pagar` llama al adaptador **sin conexión de base abierta** y, en otra transacción, aplica el
  resultado con la guarda de PENDIENTE y escribe el evento en el outbox, solo si cambió la fila y
  el estado es final. Un incierto deja la indemnización PENDIENTE con su código y sin evento: nunca
  se asume rechazo. Si la guarda no cambia la fila (la conciliación se adelantó) y la pasarela
  respondió algo definitivo, es una confirmación repetida: `registrar_duplicado`, sin otro
  movimiento ni evento.
- No lanza por fallos del adaptador; solo si falla la base. Entonces el mensaje se reentrega, llega
  como duplicado y la indemnización queda PENDIENTE para la conciliación.
- Los logs llevan la `referencia`, el siniestro y la indemnización: nunca el beneficiario ni
  cuerpos de la pasarela.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from uuid import UUID

from solventa_common.telemetry import operacion

from dinero.application.indemnizacion.puertos import (
    DesembolsoConfirmado,
    DesembolsoRechazado,
    DesembolsosPuerto,
    EventosIndemnizacion,
    IndemnizacionesRepositorio,
    Reloj,
    ResultadoDesembolso,
    SolicitudDesembolso,
    Transacciones,
)
from dinero.domain.indemnizacion.errores import IndemnizacionInvalida
from dinero.domain.indemnizacion.modelos import (
    EstadoIndemnizacion,
    Indemnizacion,
    motivo_de_rechazo,
)

Continuacion = Callable[[], Awaitable[None]]

OPERACION = "indemnizacion.pagar"
MENSAJE_YA_REGISTRADA = "indemnizacion: el siniestro ya tiene indemnización; no se paga"
MENSAJE_RESUELTA = "indemnizacion: pago resuelto"
MENSAJE_INCIERTO = "indemnizacion: pago incierto; queda PENDIENTE"
MENSAJE_YA_RESUELTA = "indemnizacion: otro proceso ya la resolvió; confirmación repetida"

logger = logging.getLogger("dinero.indemnizacion")


class AprobacionInvalida(ValueError):
    """El siniestro aprobado no cumple una invariante. `campo` lo nombra, nunca su valor."""

    def __init__(self, mensaje: str) -> None:
        super().__init__(mensaje)
        self.campo = mensaje.split(":", 1)[0]


@dataclass(frozen=True, slots=True)
class SiniestroAprobado:
    """Lo que dinero usa de `SiniestroAprobado` v1. `beneficiario_ref` es una referencia opaca."""

    siniestro_id: UUID
    referencia: str
    monto: Decimal
    moneda: str
    beneficiario_ref: str
    correlation_id: str


def _ids(indemnizacion: Indemnizacion) -> dict[str, str]:
    return {
        "indemnizacion_id": str(indemnizacion.indemnizacion_id),
        "siniestro_id": str(indemnizacion.siniestro_id),
        "referencia": indemnizacion.referencia,
    }


class PagarIndemnizacion[T]:
    def __init__(
        self,
        *,
        indemnizaciones: IndemnizacionesRepositorio[T],
        transacciones: Transacciones[T],
        eventos: EventosIndemnizacion[T],
        pagos: DesembolsosPuerto,
        reloj: Reloj,
    ) -> None:
        self._indemnizaciones = indemnizaciones
        self._transacciones = transacciones
        self._eventos = eventos
        self._pagos = pagos
        self._reloj = reloj

    async def registrar(self, aprobado: SiniestroAprobado, tx: T) -> Continuacion | None:
        """Crea la indemnización PENDIENTE en `tx`; la continuación paga solo si es nueva."""
        try:
            pendiente = Indemnizacion.pendiente(
                siniestro_id=aprobado.siniestro_id,
                referencia=aprobado.referencia,
                monto=aprobado.monto,
                moneda=aprobado.moneda,
                beneficiario_ref=aprobado.beneficiario_ref,
                correlation_id=aprobado.correlation_id,
                ahora=self._reloj.ahora(),
            )
        except IndemnizacionInvalida as error:
            raise AprobacionInvalida(str(error)) from None
        registro = await self._indemnizaciones.registrar_pendiente(pendiente, tx)
        if not registro.nuevo:
            existente = registro.indemnizacion
            logger.info(
                MENSAJE_YA_REGISTRADA,
                extra={**_ids(existente), "estado": existente.estado.value},
            )
            return None
        return partial(self.pagar, registro.indemnizacion)

    async def pagar(self, indemnizacion: Indemnizacion) -> None:
        """Paga una indemnización PENDIENTE y registra el resultado con su evento, si es final."""
        with operacion(OPERACION) as medicion:
            resultado = await self._pagos.desembolsar(
                SolicitudDesembolso(
                    referencia=indemnizacion.referencia,
                    monto=indemnizacion.monto,
                    moneda=indemnizacion.moneda.value,
                    beneficiario_ref=indemnizacion.beneficiario_ref,
                    correlation_id=indemnizacion.correlation_id,
                )
            )
            resuelta, cambiadas = await aplicar_resultado(
                indemnizacion,
                resultado,
                indemnizaciones=self._indemnizaciones,
                transacciones=self._transacciones,
                eventos=self._eventos,
                reloj=self._reloj,
                intento=True,
            )
            medicion.marcar(_clase(resuelta, resultado, cambiadas))
            medicion.anotar(**_ids(resuelta))
        if not cambiadas:
            logger.info(MENSAJE_YA_RESUELTA, extra=_ids(resuelta))
        elif resuelta.final:
            logger.info(
                MENSAJE_RESUELTA,
                extra={
                    **_ids(resuelta),
                    "estado": resuelta.estado.value,
                    "motivo": resuelta.motivo,
                },
            )
        else:
            logger.warning(
                MENSAJE_INCIERTO,
                extra={
                    **_ids(resuelta),
                    "estado": resuelta.estado.value,
                    "codigo": resuelta.ultimo_incierto,
                },
            )


def _clase(resuelta: Indemnizacion, resultado: ResultadoDesembolso, cambiadas: int) -> str:
    """El resultado de `indemnizacion.pagar` (contracts/operaciones.md)."""
    if not cambiadas:
        # Una respuesta definitiva sobre una fila ya resuelta es una confirmación repetida; un
        # incierto sobre una fila ya resuelta no cambia nada.
        definitiva = isinstance(resultado, DesembolsoConfirmado | DesembolsoRechazado)
        return "duplicado" if definitiva else "pendiente"
    if resuelta.final:
        confirmada = resuelta.estado is EstadoIndemnizacion.CONFIRMADA
        return "confirmada" if confirmada else "rechazada"
    return "pendiente"


async def aplicar_resultado[T](
    indemnizacion: Indemnizacion,
    resultado: ResultadoDesembolso,
    *,
    indemnizaciones: IndemnizacionesRepositorio[T],
    transacciones: Transacciones[T],
    eventos: EventosIndemnizacion[T],
    reloj: Reloj,
    intento: bool,
) -> tuple[Indemnizacion, int]:
    """Aplica el resultado con la guarda de PENDIENTE y, en la misma transacción, escribe el evento
    si cambió la fila y el estado es final. Si la guarda no cambió la fila y el resultado era
    definitivo, cuenta un duplicado descartado (sin otro movimiento). La usan la continuación y la
    conciliación. `intento` es verdadero si el resultado salió de un `POST` de pago. Devuelve la
    indemnización resuelta y las filas cambiadas."""
    resuelta = _aplicar(indemnizacion, resultado, reloj, intento)
    async with transacciones.transaccion() as tx:
        cambiadas = await indemnizaciones.registrar_resultado(resuelta, tx)
        if cambiadas and resuelta.final:
            await eventos.agregar(resuelta, tx)
        elif not cambiadas and isinstance(resultado, DesembolsoConfirmado | DesembolsoRechazado):
            await indemnizaciones.descartar_duplicado(resuelta.indemnizacion_id, tx)
    return resuelta, cambiadas


def _aplicar(
    indemnizacion: Indemnizacion, resultado: ResultadoDesembolso, reloj: Reloj, intento: bool
) -> Indemnizacion:
    ahora = reloj.ahora()
    if isinstance(resultado, DesembolsoConfirmado):
        return indemnizacion.confirmar(resultado.transaccion_id, ahora, intento=intento)
    if isinstance(resultado, DesembolsoRechazado):
        return indemnizacion.rechazar(
            motivo_de_rechazo(resultado.motivo),
            ahora,
            transaccion_id=resultado.transaccion_id,
            intento=intento,
        )
    return indemnizacion.registrar_incierto(resultado.codigo, ahora, intento=intento)
