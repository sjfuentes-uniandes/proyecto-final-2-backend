"""Perfilamiento del portal: reenvía a cotizacion y estabiliza sus errores (04 §B.4.3).

- Los errores B2 de cotizacion pasan con su estado y su cuerpo (el correlationId lo pone
  el BFF desde su contexto).
- Un timeout, un error de conexión o un 5xx que no sea un B2 de dependencia (500, 502, 504
  o sin cuerpo B2) se convierte en 503 SERVICIO_NO_DISPONIBLE, sin URL ni nombre interno.
- HU-W12: recálculo (reenvío) y comparación del perfil anterior con el recalculado (05 §B.4.2).
"""

from collections.abc import Awaitable
from typing import Any
from uuid import UUID

import httpx
from solventa_common.errors import ErrorNegocio

from bff_web.adapters.outbound.cotizacion import CotizacionCliente, ErrorCotizacion

SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"
SIN_PERFIL_ANTERIOR = "SIN_PERFIL_ANTERIOR"
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


def comparar(anterior: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """Compone {anterior, actual}. Los indicadores de cambio los calcula la web (W12-P03)."""
    if actual.get("perfilAnteriorId") != anterior.get("id"):
        raise ValueError("el perfil actual no se recalculó desde el anterior")
    return {"anterior": anterior, "actual": actual}


class Perfilamiento:
    def __init__(self, cotizacion: CotizacionCliente) -> None:
        self.cotizacion = cotizacion

    async def crear(self, cuerpo: dict[str, Any], actor: str, clave: UUID) -> tuple[int, dict[str, Any]]:
        return await _proteger(self.cotizacion.crear(cuerpo, actor, clave))

    async def obtener(self, perfil_id: UUID, actor: str) -> dict[str, Any]:
        return await _proteger(self.cotizacion.obtener(perfil_id, actor))

    async def listar(self, cliente_id: UUID, limite: int, actor: str) -> dict[str, Any]:
        return await _proteger(self.cotizacion.listar(cliente_id, limite, actor))

    async def recalcular(self, perfil_id: UUID, actor: str, clave: UUID) -> tuple[int, dict[str, Any]]:
        return await _proteger(self.cotizacion.recalcular(perfil_id, actor, clave))

    async def comparacion(self, perfil_id: UUID, actor: str) -> dict[str, Any]:
        actual = await _proteger(self.cotizacion.obtener(perfil_id, actor))
        anterior_id = actual.get("perfilAnteriorId")
        if anterior_id is None:
            raise ErrorNegocio(SIN_PERFIL_ANTERIOR, "El perfil no proviene de un recálculo", 422)
        anterior = await _proteger(self.cotizacion.obtener(UUID(anterior_id), actor))
        return comparar(anterior, actual)
