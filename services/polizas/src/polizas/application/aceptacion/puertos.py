"""Puertos de la aceptación (HU-W15, W15-B03). La decisión y el reloj usan los de la suscripción."""

from typing import Protocol
from uuid import UUID

from polizas.domain.aceptacion.modelos import Aceptacion
from polizas.domain.suscripcion.modelos import Decision


class AceptacionesRepositorio(Protocol):
    async def guardar(self, aceptacion: Aceptacion, decision: Decision) -> bool:
        """Inserta la aceptación y su evento OfertaAceptada en una transacción. False si ya
        existía una aceptación de la cotización o de la clave (ganó otra solicitud): no inserta
        nada."""
        ...

    async def por_cotizacion(self, cotizacion_id: UUID) -> Aceptacion | None: ...

    async def por_idempotencia(self, clave: UUID) -> Aceptacion | None: ...
