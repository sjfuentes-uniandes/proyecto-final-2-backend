"""Alta y verificación de identidad del cliente desde el portal (HU-W30).

- Los errores de clientes con `codigo` pasan con su estado y su cuerpo (el correlationId lo pone
  el BFF desde su contexto). El 503 DEPENDENCIA_NO_DISPONIBLE conserva `detalles.estado`: la
  falla del proveedor no es un rechazo y el portal puede ofrecer reintentar.
- Un timeout, un error de conexión o un 5xx sin cuerpo de negocio se convierte en 503
  SERVICIO_NO_DISPONIBLE, sin URL ni nombre interno.
"""

from collections.abc import Awaitable
from typing import Any
from uuid import UUID

import httpx
from solventa_common.errors import ErrorNegocio

from bff_web.adapters.outbound.clientes import ClientesCliente, ErrorClientes

SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"
# 503 de clientes = DEPENDENCIA_NO_DISPONIBLE (el proveedor de identidad): pasa igual.
_REENVIADOS_5XX = {503}


def _servicio_no_disponible() -> ErrorNegocio:
    return ErrorNegocio(SERVICIO_NO_DISPONIBLE, "El servicio de clientes no está disponible", 503)


async def _proteger[T](llamada: Awaitable[T]) -> T:
    try:
        return await llamada
    except ErrorClientes as error:
        codigo = error.cuerpo.get("codigo")
        if isinstance(codigo, str) and (error.status < 500 or error.status in _REENVIADOS_5XX):
            detalles = error.cuerpo.get("detalles")
            raise ErrorNegocio(
                codigo,
                str(error.cuerpo.get("mensaje") or "Solicitud rechazada"),
                error.status,
                detalles if isinstance(detalles, dict) else {},
            ) from error
        raise _servicio_no_disponible() from error
    except httpx.HTTPError as error:
        raise _servicio_no_disponible() from error


class Clientes:
    def __init__(self, clientes: ClientesCliente) -> None:
        self.clientes = clientes

    async def crear(self, cuerpo: dict[str, Any], actor: str) -> tuple[int, dict[str, Any]]:
        return await _proteger(self.clientes.crear(cuerpo, actor))

    async def obtener(self, cliente_id: UUID, actor: str) -> dict[str, Any]:
        return await _proteger(self.clientes.obtener(cliente_id, actor))

    async def verificar(self, cliente_id: UUID, actor: str) -> dict[str, Any]:
        return await _proteger(self.clientes.verificar(cliente_id, actor))
