"""Puerto de salida hacia el proveedor de notificaciones push."""

from typing import Protocol

from adaptador_firma.domain.notificaciones.modelos import Push, SolicitudPush


class PuertoProveedorPush(Protocol):
    async def enviar(self, solicitud: SolicitudPush, timeout_s: float) -> Push: ...
