"""Perfilamiento del portal: reenvía a cotizacion y estabiliza sus errores (04 §B.4.3).

- Los errores B2 de cotizacion pasan con su estado y su cuerpo (el correlationId lo pone
  el BFF desde su contexto).
- Un timeout, un error de conexión o un 5xx que no sea un B2 de dependencia (500, 502, 504
  o sin cuerpo B2) se convierte en 503 SERVICIO_NO_DISPONIBLE, sin URL ni nombre interno.
"""

from collections.abc import Awaitable
from typing import Any
from uuid import UUID

import httpx
from solventa_common.errors import ErrorNegocio

from bff_web.adapters.outbound.cotizacion import CotizacionCliente, ErrorCotizacion

SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"
# 503 de cotizacion = DEPENDENCIA_NO_DISPONIBLE (clientes o catalogo): pasa igual (§B.7).
_REENVIADOS_5XX = {503}


def _servicio_no_disponible() -> ErrorNegocio:
    return ErrorNegocio(SERVICIO_NO_DISPONIBLE, "El servicio de perfilamiento no está disponible", 503)


async def _proteger[T](llamada: Awaitable[T]) -> T:
    try:
        return await llamada
    except ErrorCotizacion as error:
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


class Perfilamiento:
    def __init__(self, cotizacion: CotizacionCliente) -> None:
        self.cotizacion = cotizacion

    async def crear(self, cuerpo: dict[str, Any], actor: str, clave: UUID) -> tuple[int, dict[str, Any]]:
        return await _proteger(self.cotizacion.crear(cuerpo, actor, clave))

    async def obtener(self, perfil_id: UUID, actor: str) -> dict[str, Any]:
        return await _proteger(self.cotizacion.obtener(perfil_id, actor))

    async def listar(self, cliente_id: UUID, limite: int, actor: str) -> dict[str, Any]:
        return await _proteger(self.cotizacion.listar(cliente_id, limite, actor))
