"""Caso de uso ProyectarRecaudo (HU-W16, 16.13): guarda el primer estado final del recaudo.

Corre en la transacción del inbox (modo a): la fila del inbox y la proyección se confirman juntas.
- Se queda el **primer** estado final por `claveRecaudo` (el orden de los eventos no se garantiza).
  Uno igual que llega después (otro `eventId`) no cambia nada. Uno **distinto** tampoco, pero se
  registra un ERROR: dinero no debería publicar dos estados finales para la misma clave.
- Una clave sin aceptación en polizas se guarda igual, sin llave hacia `aceptaciones` (la QA de la
  16.12 publica eventos sintéticos), y se registra un WARNING.
- HU-W17 (17.6): el primer `CONFIRMADO` de una aceptación conocida crea su póliza `EMITIENDO` en la
  misma transacción, con el número ya reservado. Un rechazo, un duplicado o una clave desconocida
  no crean nada. Un producto sin ramo tampoco: se registra un ERROR y el recaudo se proyecta igual
  (reintentar no lo arreglaría).

Los logs llevan la clave y los estados; nunca montos, transacciones ni motivos.
"""

import logging

from polizas.application.poliza.puertos import PolizasRepositorio
from polizas.application.recaudo.puertos import ProyeccionRecaudos
from polizas.application.suscripcion.puertos import Reloj
from polizas.domain.poliza.errores import ProductoSinRamo
from polizas.domain.poliza.modelos import OrigenPoliza, Poliza, ramo_de
from polizas.domain.recaudo.modelos import ResultadoRecaudo, SituacionRecaudo

MENSAJE_CLAVE_DESCONOCIDA = "recaudo: estado de una clave sin aceptación; se guarda igual"
MENSAJE_CONFLICTO = "recaudo: llegó otro estado final para la misma clave; se conserva el primero"
MENSAJE_PRODUCTO_SIN_RAMO = "emision: el producto no tiene ramo; no se crea la póliza"

logger = logging.getLogger("polizas.recaudo")


class ProyectarRecaudo[T]:
    def __init__(
        self, proyeccion: ProyeccionRecaudos[T], polizas: PolizasRepositorio[T], reloj: Reloj
    ) -> None:
        self._proyeccion = proyeccion
        self._polizas = polizas
        self._reloj = reloj

    async def ejecutar(self, resultado: ResultadoRecaudo, tx: T) -> None:
        clave = str(resultado.clave_recaudo)
        previo = await self._proyeccion.guardar(resultado, tx)
        if previo is None:
            origen = await self._polizas.origen(resultado.clave_recaudo, tx)
            if origen is None:
                logger.warning(
                    MENSAJE_CLAVE_DESCONOCIDA,
                    extra={"clave_recaudo": clave, "estado": resultado.estado.value},
                )
            elif resultado.estado is SituacionRecaudo.CONFIRMADO:
                await self._crear_poliza(origen, tx)
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

    async def _crear_poliza(self, origen: OrigenPoliza, tx: T) -> None:
        try:
            # Antes de reservar el número: un producto sin ramo no consume la secuencia.
            ramo_de(origen.producto.id)
        except ProductoSinRamo as error:
            logger.error(
                MENSAJE_PRODUCTO_SIN_RAMO,
                extra={
                    "clave_recaudo": str(origen.aceptacion_id),
                    "producto_id": error.producto_id,
                    "codigo": error.codigo,
                },
            )
            return
        secuencia = await self._polizas.siguiente_secuencia(tx)
        poliza = Poliza.emitiendo(origen, secuencia=secuencia, ahora=self._reloj.ahora())
        # False solo si la aceptación ya tenía póliza: se conserva esa.
        await self._polizas.crear(poliza, tx)
