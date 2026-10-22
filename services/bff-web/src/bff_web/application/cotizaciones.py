"""Cotización desde el portal (HU-W03): reenvía a cotizacion y estabiliza sus errores igual que el
perfilamiento (04 §B.4.3): los B2 de cotizacion pasan con su estado y su cuerpo; un timeout, un
error de conexión o un 5xx que no sea DEPENDENCIA_NO_DISPONIBLE se vuelve 503
SERVICIO_NO_DISPONIBLE, sin URL ni nombre interno.
"""

from typing import Any
from uuid import UUID

from bff_web.adapters.outbound.cotizacion import CotizacionCliente
from bff_web.application.perfilamiento import _proteger

MENSAJE = "El servicio de cotización no está disponible"


class Cotizaciones:
    def __init__(self, cotizacion: CotizacionCliente) -> None:
        self.cotizacion = cotizacion

    async def cotizar(
        self, cuerpo: dict[str, Any], actor: str, clave: UUID
    ) -> tuple[int, dict[str, Any]]:
        return await _proteger(self.cotizacion.cotizar(cuerpo, actor, clave), MENSAJE)

    async def obtener(self, cotizacion_id: UUID, actor: str) -> dict[str, Any]:
        return await _proteger(self.cotizacion.cotizacion(cotizacion_id, actor), MENSAJE)

    async def condiciones(self, producto_id: str, actor: str) -> dict[str, Any]:
        return await _proteger(self.cotizacion.condiciones(producto_id, actor), MENSAJE)
