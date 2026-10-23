"""Cotizar y consultar la oferta desde el canal del socio (HU-W04).

`cotizacion` calcula la prima y crea la oferta con el socio como actor (`X-Actor-Rol: SOCIO`): solo
cotiza para él y solo le deja leer sus cotizaciones. Aquí se aplica la regla del canal (una oferta
vencida no se entrega) y se mide cada operación (`socio.cotizar`, `socio.consultar_oferta`).
Los logs llevan solo identificadores: nunca la fecha de nacimiento ni la suma asegurada.
"""

import logging
from typing import Protocol
from uuid import UUID

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from api_socios.domain.ofertas import (
    CotizacionRechazada,
    Oferta,
    OfertaNoEncontrada,
    OfertaVencida,
    SolicitudOferta,
    asegurar_presentable,
)

logger = logging.getLogger("api_socios.ofertas")

RECHAZOS = (CotizacionRechazada, OfertaNoEncontrada, OfertaVencida)


class PuertoCotizacion(Protocol):
    async def cotizar(
        self, solicitud: SolicitudOferta, socio_id: str, clave: UUID
    ) -> tuple[Oferta, bool]:
        """La oferta y si se creó ahora (False: la misma Idempotency-Key ya la había creado)."""
        ...

    async def oferta(self, cotizacion_id: UUID, socio_id: str) -> Oferta:
        """La oferta de una cotización del socio. Lanza OfertaNoEncontrada."""
        ...


class CotizarOferta:
    def __init__(self, cotizacion: PuertoCotizacion) -> None:
        self._cotizacion = cotizacion

    async def ejecutar(
        self, socio_id: str, solicitud: SolicitudOferta, clave: UUID
    ) -> tuple[Oferta, bool]:
        with operacion("socio.cotizar") as op:
            try:
                oferta, creada = await self._cotizacion.cotizar(solicitud, socio_id, clave)
                asegurar_presentable(oferta)
            except RECHAZOS:
                op.marcar(RESULTADO_RECHAZADO)
                raise
        logger.info(
            "oferta entregada al socio",
            extra={"socio_id": socio_id, "oferta_id": str(oferta.id), "creada": creada},
        )
        return oferta, creada


class ConsultarOferta:
    def __init__(self, cotizacion: PuertoCotizacion) -> None:
        self._cotizacion = cotizacion

    async def ejecutar(self, socio_id: str, cotizacion_id: UUID) -> Oferta:
        with operacion("socio.consultar_oferta") as op:
            try:
                return asegurar_presentable(await self._cotizacion.oferta(cotizacion_id, socio_id))
            except RECHAZOS:
                op.marcar(RESULTADO_RECHAZADO)
                raise
