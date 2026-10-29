"""Cotización desde el portal (HU-W03): reenvía a cotizacion y estabiliza sus errores igual que el
perfilamiento (04 §B.4.3): los B2 de cotizacion pasan con su estado y su cuerpo; un timeout, un
error de conexión o un 5xx que no sea DEPENDENCIA_NO_DISPONIBLE se vuelve 503
SERVICIO_NO_DISPONIBLE, sin URL ni nombre interno.

"Cotizaciones abiertas": el listado de cotizacion con el nombre de cada cliente, que se pide a
clientes como en la bandeja de suscripción (HU-W13). Sin nombre a tiempo, la fila sale igual.
"""

from typing import Any
from uuid import UUID

from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.cotizacion import CotizacionCliente
from bff_web.application.nombres import nombres_de_clientes
from bff_web.application.perfilamiento import _proteger

MENSAJE = "El servicio de cotización no está disponible"


class Cotizaciones:
    def __init__(
        self,
        cotizacion: CotizacionCliente,
        clientes: ClientesCliente | None = None,
        nombre_timeout_ms: int = 500,
    ) -> None:
        self.cotizacion = cotizacion
        self.clientes = clientes
        self._nombre_timeout_s = nombre_timeout_ms / 1000

    async def cotizar(
        self, cuerpo: dict[str, Any], actor: str, clave: UUID
    ) -> tuple[int, dict[str, Any]]:
        return await _proteger(self.cotizacion.cotizar(cuerpo, actor, clave), MENSAJE)

    async def obtener(self, cotizacion_id: UUID, actor: str) -> dict[str, Any]:
        return await _proteger(self.cotizacion.cotizacion(cotizacion_id, actor), MENSAJE)

    async def condiciones(self, producto_id: str, actor: str) -> dict[str, Any]:
        return await _proteger(self.cotizacion.condiciones(producto_id, actor), MENSAJE)

    async def listar(self, parametros: dict[str, Any], actor: str) -> dict[str, Any]:
        """Una página del listado; cada fila con `clienteNombre` (null si clientes no responde)."""
        pagina: dict[str, Any] = await _proteger(
            self.cotizacion.cotizaciones(parametros, actor), MENSAJE
        )
        items = [i for i in pagina.get("items", []) if isinstance(i, dict)]
        nombres: dict[str, str | None] = {}
        if self.clientes is not None:
            nombres = await nombres_de_clientes(
                self.clientes, [i.get("clienteId") for i in items], actor, self._nombre_timeout_s
            )
        return {
            **pagina,
            "items": [{**i, "clienteNombre": nombres.get(str(i.get("clienteId")))} for i in items],
        }
