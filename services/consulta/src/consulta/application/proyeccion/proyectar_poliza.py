"""Caso de uso ProyectarPoliza (HU-M10): copia la póliza emitida a la proyección de lectura.

Corre en la transacción del inbox. La póliza `ACTIVA` es final e inmutable, así que la inserción es
idempotente por `polizaId`: si ya existe, no se modifica (`emitida_en` es la fecha del documento).
El ramo y las coberturas se copian del catálogo como foto fija; si la versión vigente no es la del
evento, lanza `VersionProductoDistinta` para que el mensaje se reintente o vaya a la DLQ.

Los logs llevan ids y estados; nunca el número, la huella ni el cliente completo.
"""

import logging
from datetime import datetime
from decimal import Decimal

from consulta.application.proyeccion.comandos import PolizaEmitida
from consulta.application.proyeccion.puertos import (
    ConsultaCatalogo,
    ProductoCatalogo,
    ProyeccionPolizas,
    Reloj,
    VersionProductoDistinta,
)
from consulta.domain.polizas.modelos import PolizaProyectada, Vigencia

MENSAJE_YA_PROYECTADA = "poliza: ya estaba proyectada; no se modifica"
MENSAJE_VERSION_DISTINTA = "poliza: la versión del producto no es la vigente del catálogo"

logger = logging.getLogger("consulta.proyeccion")


def proyectada_de(
    evento: PolizaEmitida, producto: ProductoCatalogo, proyectada_en: datetime
) -> PolizaProyectada:
    return PolizaProyectada(
        poliza_id=evento.poliza_id,
        cliente_id=evento.cliente_id,
        numero=evento.numero,
        estado="ACTIVA",
        producto_id=evento.producto_id,
        producto_version=evento.producto_version,
        producto_nombre=producto.nombre,
        ramo=producto.ramo,
        vigencia=Vigencia(evento.vigencia_desde, evento.vigencia_hasta),
        prima=Decimal(evento.prima),
        suma_asegurada=Decimal(evento.suma_asegurada),
        moneda=evento.moneda,
        periodicidad=evento.periodicidad,
        coberturas=producto.coberturas,
        documento_huella=evento.documento_huella,
        firma_id=evento.firma_id,
        emitida_en=evento.emitida_en,
        proyectada_en=proyectada_en,
    )


class ProyectarPoliza[T]:
    def __init__(
        self, catalogo: ConsultaCatalogo, proyeccion: ProyeccionPolizas[T], reloj: Reloj
    ) -> None:
        self._catalogo = catalogo
        self._proyeccion = proyeccion
        self._reloj = reloj

    async def ejecutar(self, evento: PolizaEmitida, tx: T) -> None:
        producto = await self._catalogo.producto(evento.producto_id)
        if producto.version != evento.producto_version:
            logger.warning(
                MENSAJE_VERSION_DISTINTA,
                extra={
                    "producto_id": evento.producto_id,
                    "version_evento": evento.producto_version,
                    "version_vigente": producto.version,
                },
            )
            raise VersionProductoDistinta(
                evento.producto_id, evento.producto_version, producto.version
            )
        poliza = proyectada_de(evento, producto, self._reloj.ahora())
        if not await self._proyeccion.insertar(poliza, tx):
            logger.info(MENSAJE_YA_PROYECTADA, extra={"poliza_id": str(evento.poliza_id)})
