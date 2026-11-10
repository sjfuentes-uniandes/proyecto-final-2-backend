"""Conciliar las indemnizaciones PENDIENTE (HU-W23; ADR 0015, decisión 5; DS3-18; ADR 0013 dec. 5).

Una PENDIENTE queda así tras un incierto (timeout, 5xx o respuesta inválida) o si la continuación
no llegó a pagar; su mensaje ya se borró de la cola, así que nada más la resuelve. A diferencia de
la conciliación de recaudos (sin `FOR UPDATE`), aquí varias réplicas **no consultan dos veces** el
mismo movimiento:

1. **Reclama** (transacción corta): hasta `lote` PENDIENTE con `actualizado_en` anterior a
   `ahora - antiguedad_min_s` y sin reclamo vigente, con `FOR UPDATE SKIP LOCKED`, y marca
   `reclamado_hasta = ahora + reclamo_s`. Suelta la conexión.
2. **Consulta** `GET /v1/desembolsos/{referencia}?beneficiarioRef` **después del commit**.
3. **Aplica** el resultado en otra transacción corta (`aplicar_resultado`): la guarda de PENDIENTE y
   el evento, en la misma fila.
   - CONFIRMADO o RECHAZADO: se aplica sobre **la misma fila**.
   - `DESEMBOLSO_NO_ENCONTRADO`: la orden nunca llegó a la pasarela; se reintenta con la misma
     `referencia`, idempotente en la pasarela, y su resultado se aplica igual. Nunca es un rechazo.
   - Incierto (de la consulta o del reintento): sigue PENDIENTE con su código y se renueva
     `actualizado_en`, así que el próximo intento espera otra `antiguedad_min_s`.
- Un movimiento que falla (p. ej., la base) se registra y no detiene a los demás. La parada se
  atiende entre movimientos.
- Por movimiento, la operación `indemnizacion.conciliar` (contracts/operaciones.md) y un log con
  `indemnizacion_id`, `siniestro_id`, `referencia`, el resultado, el código y si hubo reintento; con
  la correlación del movimiento y nunca el beneficiario. El log y el span llevan el `tipo`
  (`INDEMNIZACION`) y la `antiguedad_s`, que es lo que HU-S03 podrá usar para extender
  `pending_collection_age_seconds` con esa dimensión.
- Un incierto con más de `alerta_tras_s` desde `creado_en` se mide como `error` (cuenta en
  `OperationErrors`) con un log ERROR. Sigue PENDIENTE y se sigue conciliando: no hay tope.
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

from dinero.application.indemnizacion.pagar import aplicar_resultado
from dinero.application.indemnizacion.puertos import (
    ConsultaDesembolso,
    DesembolsoNoEncontrado,
    DesembolsosPuerto,
    EventosIndemnizacion,
    IndemnizacionesRepositorio,
    Reloj,
    SolicitudDesembolso,
    Transacciones,
)
from dinero.domain.indemnizacion.modelos import TIPO, EstadoIndemnizacion, Indemnizacion

OPERACION = "indemnizacion.conciliar"
MENSAJE_CONCILIADA = "conciliacion: indemnizacion conciliada"
MENSAJE_FALLIDA = "conciliacion: la indemnizacion no se pudo conciliar; sigue PENDIENTE"
MENSAJE_CICLO_FALLIDO = "conciliacion: el ciclo de indemnizaciones falló; se reintenta"
MENSAJE_VENCIDA = "conciliacion: indemnizacion PENDIENTE vencida; se sigue conciliando"

logger = logging.getLogger("dinero.conciliacion")


class Resultado(StrEnum):
    """El resultado de la operación `indemnizacion.conciliar` por movimiento."""

    CONFIRMADA = "confirmada"
    RECHAZADA = "rechazada"
    INCIERTO = "incierto"
    # La guarda no cambió la fila: la continuación u otra réplica ya la resolvió.
    YA_RESUELTO = "ya_resuelto"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Conciliada:
    indemnizacion_id: UUID
    siniestro_id: UUID
    resultado: Resultado
    # El código del incierto o el motivo del rechazo; nulo en lo demás.
    codigo: str | None
    reintento: bool
    # Incierto con más de `alerta_tras_s` desde `creado_en`: se midió como `error`.
    vencido: bool = False


def _resultado(resuelta: Indemnizacion, cambiadas: int) -> tuple[Resultado, str | None]:
    if not cambiadas:
        return Resultado.YA_RESUELTO, None
    if resuelta.estado is EstadoIndemnizacion.CONFIRMADA:
        return Resultado.CONFIRMADA, None
    if resuelta.estado is EstadoIndemnizacion.RECHAZADA:
        return Resultado.RECHAZADA, resuelta.motivo
    return Resultado.INCIERTO, resuelta.ultimo_incierto


def _ids(indemnizacion: Indemnizacion) -> dict[str, str]:
    return {
        "indemnizacion_id": str(indemnizacion.indemnizacion_id),
        "siniestro_id": str(indemnizacion.siniestro_id),
        "referencia": indemnizacion.referencia,
    }


class ConciliarIndemnizaciones[T]:
    def __init__(
        self,
        *,
        indemnizaciones: IndemnizacionesRepositorio[T],
        transacciones: Transacciones[T],
        eventos: EventosIndemnizacion[T],
        pagos: DesembolsosPuerto,
        reloj: Reloj,
        intervalo_s: float,
        antiguedad_min_s: float,
        lote: int,
        reclamo_s: float,
        alerta_tras_s: float,
    ) -> None:
        self._indemnizaciones = indemnizaciones
        self._transacciones = transacciones
        self._eventos = eventos
        self._pagos = pagos
        self._reloj = reloj
        self._intervalo_s = intervalo_s
        self._antiguedad = timedelta(seconds=antiguedad_min_s)
        self._lote = lote
        self._reclamo = timedelta(seconds=reclamo_s)
        self._alerta_tras = timedelta(seconds=alerta_tras_s)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        """Espera el intervalo y concilia, hasta la parada. Espera antes del primer ciclo: las
        réplicas que arrancan juntas (un despliegue) no consultan a la vez."""
        while not await _parada_dentro_de(parada, self._intervalo_s):
            try:
                await self.conciliar_una_vez(parada)
            except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
                logger.error(MENSAJE_CICLO_FALLIDO, extra={"error_type": type(error).__name__})

    async def conciliar_una_vez(self, parada: asyncio.Event | None = None) -> list[Conciliada]:
        """Un ciclo. Lo invocan el bucle y las pruebas. Reclama el lote (y suelta la conexión)
        antes de consultar a la pasarela."""
        ahora = self._reloj.ahora()
        reclamadas = await self._indemnizaciones.reclamar(
            ahora - self._antiguedad, ahora, ahora + self._reclamo, self._lote
        )
        conciliadas: list[Conciliada] = []
        for indemnizacion in reclamadas:
            if parada is not None and parada.is_set():
                break
            conciliadas.append(await self._conciliar_aislada(indemnizacion))
        return conciliadas

    async def _conciliar_aislada(self, indemnizacion: Indemnizacion) -> Conciliada:
        token = correlation.establecer(indemnizacion.correlation_id)
        try:
            return await self._conciliar(indemnizacion)
        except Exception as error:  # noqa: BLE001 - un movimiento no detiene a los demás
            logger.error(
                MENSAJE_FALLIDA,
                extra={**_ids(indemnizacion), "error_type": type(error).__name__},
            )
            return Conciliada(
                indemnizacion.indemnizacion_id,
                indemnizacion.siniestro_id,
                Resultado.ERROR,
                None,
                reintento=False,
            )
        finally:
            correlation.restablecer(token)

    async def _conciliar(self, indemnizacion: Indemnizacion) -> Conciliada:
        antiguedad = self._reloj.ahora() - indemnizacion.creado_en
        with operacion(OPERACION) as medicion:
            medicion.anotar(tipo=TIPO, antiguedad_s=int(antiguedad.total_seconds()))
            consulta = await self._pagos.consultar_desembolso(
                ConsultaDesembolso(
                    referencia=indemnizacion.referencia,
                    beneficiario_ref=indemnizacion.beneficiario_ref,
                    correlation_id=indemnizacion.correlation_id,
                )
            )
            reintento = isinstance(consulta, DesembolsoNoEncontrado)
            if isinstance(consulta, DesembolsoNoEncontrado):
                resultado = await self._pagos.desembolsar(
                    SolicitudDesembolso(
                        referencia=indemnizacion.referencia,
                        monto=indemnizacion.monto,
                        moneda=indemnizacion.moneda.value,
                        beneficiario_ref=indemnizacion.beneficiario_ref,
                        correlation_id=indemnizacion.correlation_id,
                    )
                )
            else:
                resultado = consulta
            resuelta, cambiadas = await aplicar_resultado(
                indemnizacion,
                resultado,
                indemnizaciones=self._indemnizaciones,
                transacciones=self._transacciones,
                eventos=self._eventos,
                reloj=self._reloj,
                # Solo el reintento del desembolso es un intento de pago; la consulta no.
                intento=reintento,
            )
            clase, codigo = _resultado(resuelta, cambiadas)
            # Solo `error` y `timeout` suman en OperationErrors (telemetry.registrar), que vigila
            # la alarma de tasa de error del servicio. Sigue PENDIENTE: sin tope.
            vencido = clase is Resultado.INCIERTO and antiguedad > self._alerta_tras
            medicion.marcar(Resultado.ERROR.value if vencido else clase.value)
        ids = _ids(indemnizacion)
        if vencido:
            logger.error(
                MENSAJE_VENCIDA,
                extra={
                    **ids,
                    "codigo": codigo,
                    "reintento": reintento,
                    "antiguedad_s": int(antiguedad.total_seconds()),
                },
            )
            return Conciliada(
                indemnizacion.indemnizacion_id,
                indemnizacion.siniestro_id,
                Resultado.ERROR,
                codigo,
                reintento,
                vencido=True,
            )
        logger.info(
            MENSAJE_CONCILIADA,
            extra={
                **ids,
                "resultado": clase.value,
                "codigo": codigo,
                "reintento": reintento,
            },
        )
        return Conciliada(
            indemnizacion.indemnizacion_id, indemnizacion.siniestro_id, clase, codigo, reintento
        )


async def _parada_dentro_de(parada: asyncio.Event, segundos: float) -> bool:
    """True si llega la parada antes de `segundos`."""
    with suppress(TimeoutError):
        await asyncio.wait_for(parada.wait(), segundos)
    return parada.is_set()
