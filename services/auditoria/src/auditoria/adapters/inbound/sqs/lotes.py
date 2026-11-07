"""Consumidor por lotes de la cola `auditoria` (HU-W24, D3).

`auditoria` recibe todos los eventos (cola sin filtro, `catalog.tf`; `modelo_informacion.puml`:
`CanalEventos --> HistorialAuditoria`). En una avalancha, un objeto por evento la saturaría; por eso
cada lote recibido (hasta 10) es **un objeto** en S3 (`RegistrarLote`), y escala por la profundidad
de su cola (política `backlog` de `infra/apps/autoscaling.tf`).

- Recibe con espera larga de 20 s y lote de 10 (`ReceptorSqs.recibir_hasta`). Un lote a la vez por
  tarea: la cadena se escribe en serie de todos modos (bloqueo de la cabeza).
- Lee cada sobre con `solventa_common.inbox.leer_sobre`. Un mensaje envenenado (sin sobre, o sin
  `aggregateId` u `occurredAt`) no se registra ni se borra: tras 5 recepciones va a la DLQ. El resto
  del lote sigue.
- Borra los mensajes del lote (nuevos y duplicados) **después** del commit. Si el registro falla,
  rollback y no se borra nada: el lote vuelve tras la visibilidad (60 s).
- Sin latido: un lote es una escritura en S3 y una transacción corta, muy por debajo de los 60 s.
- Mide cada lote con `operacion("auditoria.lote")`: mensajes, nuevos, duplicados, envenenados y
  secuencia. Los logs no llevan cuerpos.
- Parada: deja de recibir (una espera larga en curso se abandona y lo que traiga se libera) y
  termina el lote en curso.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from solventa_common.inbox import (
    LOTE_SQS,
    EventoRecibido,
    MensajeSqs,
    ReceptorControlado,
    SobreInvalido,
    leer_sobre,
)
from solventa_common.outbox import describir_error, espera_tras_fallos
from solventa_common.telemetry import RESULTADO_ERROR, operacion

from auditoria.application.registrar_lote import Resultado
from auditoria.domain.registro import EventoAuditable, actor_de

OPERACION = "auditoria.lote"
MOTIVO_INCOMPLETO = "sobre_incompleto"
MENSAJE_ENVENENADO = "auditoria: mensaje envenenado; irá a la DLQ"
MENSAJE_RECEPCION_FALLIDA = "auditoria: la recepción falló"
MENSAJE_BORRADO_FALLIDO = "auditoria: no se pudo borrar el mensaje"
MENSAJE_LIBERACION_FALLIDA = "auditoria: no se pudo liberar el mensaje"
INTERVALO_S = 1.0
INTERVALO_MAX_S = 60.0

logger = logging.getLogger("auditoria.lotes")

Registrar = Callable[[Sequence[EventoAuditable]], Awaitable[Resultado]]


def auditable(evento: EventoRecibido) -> EventoAuditable:
    """Lo que se audita de un sobre; `SobreInvalido` si falta `aggregateId` u `occurredAt`."""
    aggregate_id = evento.sobre.get("aggregateId")
    ocurrido = evento.sobre.get("occurredAt")
    if not isinstance(aggregate_id, str) or not isinstance(ocurrido, str):
        raise SobreInvalido(MOTIVO_INCOMPLETO)
    return EventoAuditable(
        event_id=str(evento.event_id),
        event_type=evento.event_type,
        aggregate_id=aggregate_id,
        occurred_at=ocurrido,
        correlation_id=evento.correlation_id,
        actor=actor_de(evento.sobre.get("payload")),
        cuerpo=evento.cuerpo,
    )


async def _esperar(parada: asyncio.Event, segundos: float) -> None:
    try:
        await asyncio.wait_for(parada.wait(), segundos)
    except TimeoutError:
        pass


class ConsumidorLotes:
    def __init__(
        self,
        receptor: ReceptorControlado,
        registrar: Registrar,
        *,
        lote: int = LOTE_SQS,
        intervalo_s: float = INTERVALO_S,
        intervalo_max_s: float = INTERVALO_MAX_S,
    ) -> None:
        self._receptor = receptor
        self._registrar = registrar
        self._lote = lote
        self._intervalo_s = intervalo_s
        self._intervalo_max_s = intervalo_max_s

    async def ejecutar_una_vez(self) -> int:
        """Recibe y registra un lote; devuelve cuántos mensajes borró. Un fallo de recepción se
        propaga."""
        return await self._procesar(await self._receptor.recibir_hasta(self._lote))

    async def ejecutar(self, parada: asyncio.Event) -> None:
        fallos_seguidos = 0
        while not parada.is_set():
            try:
                mensajes = await self._recibir_o_parar(parada)
            except Exception as error:  # noqa: BLE001 - el siguiente ciclo lo reintenta
                fallos_seguidos += 1
                logger.error(
                    MENSAJE_RECEPCION_FALLIDA, extra={"error_type": describir_error(error)}
                )
                espera = espera_tras_fallos(
                    self._intervalo_s, self._intervalo_max_s, fallos_seguidos - 1
                )
                await _esperar(parada, espera)
                continue
            fallos_seguidos = 0
            await self._procesar(mensajes)

    async def _recibir_o_parar(self, parada: asyncio.Event) -> list[MensajeSqs]:
        recepcion = asyncio.ensure_future(self._receptor.recibir_hasta(self._lote))
        senal = asyncio.ensure_future(parada.wait())
        await asyncio.wait({recepcion, senal}, return_when=asyncio.FIRST_COMPLETED)
        senal.cancel()
        if recepcion.done():
            return recepcion.result()
        recepcion.add_done_callback(self._liberar_tardios)
        return []

    def _liberar_tardios(self, recepcion: "asyncio.Future[list[MensajeSqs]]") -> None:
        if recepcion.cancelled() or recepcion.exception() is not None:
            return
        for mensaje in recepcion.result():
            asyncio.ensure_future(self._liberar(mensaje))

    async def _liberar(self, mensaje: MensajeSqs) -> None:
        try:
            await self._receptor.liberar(mensaje)
        except Exception as error:  # noqa: BLE001 - reaparece tras la visibilidad de la cola
            logger.error(
                MENSAJE_LIBERACION_FALLIDA,
                extra={"message_id": mensaje.message_id, "error_type": describir_error(error)},
            )

    async def _procesar(self, mensajes: list[MensajeSqs]) -> int:
        if not mensajes:
            return 0
        validos: list[tuple[MensajeSqs, EventoAuditable]] = []
        envenenados = 0
        for mensaje in mensajes:
            try:
                validos.append((mensaje, auditable(leer_sobre(mensaje))))
            except SobreInvalido as invalido:
                envenenados += 1
                logger.warning(
                    MENSAJE_ENVENENADO,
                    extra={
                        "message_id": mensaje.message_id,
                        "receive_count": mensaje.recepciones,
                        "reason": invalido.motivo,
                    },
                )
        campos: dict[str, Any] = {"messages": len(mensajes), "poisoned": envenenados}
        with operacion(OPERACION) as medicion:
            if not validos:
                medicion.anotar(**campos, new=0, duplicates=0)
                return 0
            try:
                resultado = await self._registrar([evento for _, evento in validos])
            except Exception as error:  # noqa: BLE001 - el lote vuelve tras la visibilidad
                medicion.anotar(**campos, error_type=describir_error(error))
                medicion.marcar(RESULTADO_ERROR)
                return 0
            medicion.anotar(
                **campos,
                new=resultado.nuevos,
                duplicates=resultado.duplicados,
                sequence=resultado.secuencia,
            )
            await asyncio.gather(*(self._borrar(mensaje) for mensaje, _ in validos))
        return len(validos)

    async def _borrar(self, mensaje: MensajeSqs) -> None:
        try:
            await self._receptor.borrar(mensaje)
        except Exception as error:  # noqa: BLE001 - la reentrega será un duplicado
            logger.error(
                MENSAJE_BORRADO_FALLIDO,
                extra={"message_id": mensaje.message_id, "error_type": describir_error(error)},
            )
