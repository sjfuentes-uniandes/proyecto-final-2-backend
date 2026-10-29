"""Conciliar los movimientos PENDIENTE (HU-W16, 16.11; plan §d; ADR 0013, decisión 5; P16-3).

Un PENDIENTE queda así tras un incierto (timeout, 5xx o respuesta inválida) o si la continuación no
llegó a cobrar; su mensaje ya se borró de la cola, así que nada más lo resuelve.

- Cada ciclo toma hasta `lote` PENDIENTE con `actualizado_en` anterior a `ahora - antiguedad_min_s`,
  los más antiguos primero, sin bloquear filas, y consulta cada uno por su `claveRecaudo`.
- APROBADO o RECHAZADO: se aplica como en la continuación (`aplicar_resultado`): la guarda de
  PENDIENTE y el evento en la misma transacción. Si otra tarea ya lo resolvió, sin evento.
- `COBRO_NO_ENCONTRADO`: el cobro nunca llegó al proveedor; se reintenta con la misma
  `claveRecaudo`, idempotente en el proveedor, y su resultado se aplica igual. Nunca es un rechazo.
- Incierto (de la consulta o del reintento): queda PENDIENTE con su código y se renueva
  `actualizado_en`, así que el próximo intento espera otra `antiguedad_min_s`.
- Un movimiento que falla (p. ej., la base) se registra y no detiene a los demás. La parada se
  atiende entre movimientos.
- Por movimiento, la operación `recaudo.conciliar` (contracts/operaciones.md) y un log con
  `recaudo_id`, `clave_recaudo`, el resultado, el código y si hubo reintento; con la correlación
  del movimiento y nunca el pagador.
- Un incierto con más de `alerta_tras_s` desde `creado_en` se mide como `error` (cuenta en
  `OperationErrors`) con un log ERROR que lleva su antigüedad. Sigue PENDIENTE y se sigue
  conciliando: no hay tope.
"""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from uuid import UUID

from solventa_common import correlation
from solventa_common.telemetry import operacion

from dinero.application.recaudo.puertos import (
    CobroNoEncontrado,
    ConsultaCobro,
    EventosRecaudo,
    PagosPuerto,
    PendientesRepositorio,
    Reloj,
    SolicitudCobro,
    Transacciones,
)
from dinero.application.recaudo.recaudar import aplicar_resultado
from dinero.domain.recaudo.modelos import EstadoRecaudo, Recaudo

OPERACION = "recaudo.conciliar"
MENSAJE_CONCILIADO = "conciliacion: movimiento conciliado"
MENSAJE_FALLIDO = "conciliacion: el movimiento no se pudo conciliar; sigue PENDIENTE"
MENSAJE_CICLO_FALLIDO = "conciliacion: el ciclo falló; se reintenta en el siguiente"
MENSAJE_VENCIDO = "conciliacion: movimiento PENDIENTE vencido; se sigue conciliando"

logger = logging.getLogger("dinero.conciliacion")


class Resultado(StrEnum):
    """El resultado de la operación `recaudo.conciliar` por movimiento."""

    CONFIRMADO = "confirmado"
    RECHAZADO = "rechazado"
    INCIERTO = "incierto"
    # La guarda no cambió la fila: la continuación u otra conciliación ya lo resolvió.
    YA_RESUELTO = "ya_resuelto"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Conciliado:
    recaudo_id: UUID
    clave_recaudo: UUID
    resultado: Resultado
    # El código del incierto o el motivo del rechazo; nulo en lo demás.
    codigo: str | None
    reintento: bool
    # Incierto con más de `alerta_tras_s` desde `creado_en`: se midió como `error`.
    vencido: bool = False


def _resultado(resuelto: Recaudo, cambiadas: int) -> tuple[Resultado, str | None]:
    if not cambiadas:
        return Resultado.YA_RESUELTO, None
    if resuelto.estado is EstadoRecaudo.CONFIRMADO:
        return Resultado.CONFIRMADO, None
    if resuelto.estado is EstadoRecaudo.RECHAZADO:
        return Resultado.RECHAZADO, resuelto.motivo
    return Resultado.INCIERTO, resuelto.ultimo_incierto


def _ids(recaudo: Recaudo) -> dict[str, str]:
    return {"clave_recaudo": str(recaudo.clave_recaudo), "recaudo_id": str(recaudo.recaudo_id)}


class ConciliarRecaudos[T]:
    def __init__(
        self,
        *,
        recaudos: PendientesRepositorio[T],
        transacciones: Transacciones[T],
        eventos: EventosRecaudo[T],
        pagos: PagosPuerto,
        reloj: Reloj,
        intervalo_s: float,
        antiguedad_min_s: float,
        lote: int,
        alerta_tras_s: float,
    ) -> None:
        self._recaudos = recaudos
        self._transacciones = transacciones
        self._eventos = eventos
        self._pagos = pagos
        self._reloj = reloj
        self._intervalo_s = intervalo_s
        self._antiguedad = timedelta(seconds=antiguedad_min_s)
        self._lote = lote
        self._alerta_tras = timedelta(seconds=alerta_tras_s)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        """Espera el intervalo y concilia, hasta la parada. Espera antes del primer ciclo: las
        tareas que arrancan juntas (un despliegue) no consultan a la vez."""
        while not await _parada_dentro_de(parada, self._intervalo_s):
            try:
                await self.conciliar_una_vez(parada)
            except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
                logger.error(MENSAJE_CICLO_FALLIDO, extra={"error_type": type(error).__name__})

    async def conciliar_una_vez(self, parada: asyncio.Event | None = None) -> list[Conciliado]:
        """Un ciclo. Lo invocan el bucle y las pruebas."""
        antes_de = self._reloj.ahora() - self._antiguedad
        pendientes = await self._recaudos.pendientes(antes_de, self._lote)
        conciliados: list[Conciliado] = []
        for recaudo in pendientes:
            if parada is not None and parada.is_set():
                break
            conciliados.append(await self._conciliar_aislado(recaudo))
        return conciliados

    async def _conciliar_aislado(self, recaudo: Recaudo) -> Conciliado:
        token = correlation.establecer(recaudo.correlation_id)
        try:
            return await self._conciliar(recaudo)
        except Exception as error:  # noqa: BLE001 - un movimiento no detiene a los demás
            logger.error(
                MENSAJE_FALLIDO, extra={**_ids(recaudo), "error_type": type(error).__name__}
            )
            return Conciliado(
                recaudo.recaudo_id, recaudo.clave_recaudo, Resultado.ERROR, None, reintento=False
            )
        finally:
            correlation.restablecer(token)

    async def _conciliar(self, recaudo: Recaudo) -> Conciliado:
        with operacion(OPERACION) as medicion:
            consulta = await self._pagos.consultar(
                ConsultaCobro(
                    clave_recaudo=recaudo.clave_recaudo,
                    pagador_ref=recaudo.pagador_ref,
                    correlation_id=recaudo.correlation_id,
                )
            )
            reintento = isinstance(consulta, CobroNoEncontrado)
            if isinstance(consulta, CobroNoEncontrado):
                resultado = await self._pagos.cobrar(
                    SolicitudCobro(
                        clave_recaudo=recaudo.clave_recaudo,
                        monto=recaudo.monto,
                        moneda=recaudo.moneda.value,
                        pagador_ref=recaudo.pagador_ref,
                        correlation_id=recaudo.correlation_id,
                    )
                )
            else:
                resultado = consulta
            resuelto, cambiadas = await aplicar_resultado(
                recaudo,
                resultado,
                recaudos=self._recaudos,
                transacciones=self._transacciones,
                eventos=self._eventos,
                reloj=self._reloj,
            )
            clase, codigo = _resultado(resuelto, cambiadas)
            antiguedad = self._reloj.ahora() - recaudo.creado_en
            # Solo `error` y `timeout` suman en OperationErrors (telemetry.registrar), que vigila
            # la alarma de tasa de error del servicio. Sigue PENDIENTE: sin tope.
            vencido = clase is Resultado.INCIERTO and antiguedad > self._alerta_tras
            medicion.marcar(Resultado.ERROR.value if vencido else clase.value)
        if vencido:
            logger.error(
                MENSAJE_VENCIDO,
                extra={
                    **_ids(recaudo),
                    "codigo": codigo,
                    "reintento": reintento,
                    "antiguedad_s": int(antiguedad.total_seconds()),
                },
            )
            return Conciliado(
                recaudo.recaudo_id,
                recaudo.clave_recaudo,
                Resultado.ERROR,
                codigo,
                reintento,
                vencido=True,
            )
        logger.info(
            MENSAJE_CONCILIADO,
            extra={
                **_ids(recaudo),
                "resultado": clase.value,
                "codigo": codigo,
                "reintento": reintento,
            },
        )
        return Conciliado(recaudo.recaudo_id, recaudo.clave_recaudo, clase, codigo, reintento)


async def _parada_dentro_de(parada: asyncio.Event, segundos: float) -> bool:
    """True si llega la parada antes de `segundos`."""
    with suppress(TimeoutError):
        await asyncio.wait_for(parada.wait(), segundos)
    return parada.is_set()
