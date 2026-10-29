"""Caso de uso ProyectarRecaudo (HU-W16, 16.13): guarda el primer estado final del recaudo.

Corre en la transacción del inbox (modo a): la fila del inbox y la proyección se confirman juntas.
- Se queda el **primer** estado final por `claveRecaudo` (el orden de los eventos no se garantiza).
  Uno igual que llega después (otro `eventId`) no cambia nada. Uno **distinto** tampoco, pero se
  registra un ERROR: dinero no debería publicar dos estados finales para la misma clave.
- Una clave sin aceptación en polizas se guarda igual, sin llave hacia `aceptaciones` (la QA de la
  16.12 publica eventos sintéticos), y se registra un WARNING.

Los logs llevan la clave y los estados; nunca montos, transacciones ni motivos.
"""

import logging

from polizas.application.recaudo.puertos import ProyeccionRecaudos
from polizas.domain.recaudo.modelos import ResultadoRecaudo

MENSAJE_CLAVE_DESCONOCIDA = "recaudo: estado de una clave sin aceptación; se guarda igual"
MENSAJE_CONFLICTO = "recaudo: llegó otro estado final para la misma clave; se conserva el primero"

logger = logging.getLogger("polizas.recaudo")


class ProyectarRecaudo[T]:
    def __init__(self, proyeccion: ProyeccionRecaudos[T]) -> None:
        self._proyeccion = proyeccion

    async def ejecutar(self, resultado: ResultadoRecaudo, tx: T) -> None:
        clave = str(resultado.clave_recaudo)
        previo = await self._proyeccion.guardar(resultado, tx)
        if previo is None:
            if not await self._proyeccion.aceptacion_conocida(resultado.clave_recaudo, tx):
                logger.warning(
                    MENSAJE_CLAVE_DESCONOCIDA,
                    extra={"clave_recaudo": clave, "estado": resultado.estado.value},
                )
            return
        if previo.estado is not resultado.estado:
            logger.error(
                MENSAJE_CONFLICTO,
                extra={
                    "clave_recaudo": clave,
                    "estado": previo.estado.value,
                    "estado_recibido": resultado.estado.value,
                },
            )
