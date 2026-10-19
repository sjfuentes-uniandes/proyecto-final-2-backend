"""HU-W31: consentimientos del cliente para el asesor, de solo lectura (ADR 0002).

Reenvía a clientes y devuelve lo mismo: no recalcula el estado (clientes deriva VENCIDO con el
reloj de su base, ADR 0010) ni cachea (ADR 0011). Falla cerrado:

- Solo pasan, con su estado y su cuerpo, los errores que el contrato de clientes promete para
  cada ruta (el correlationId lo pone el BFF desde su contexto).
- Cualquier otra respuesta (otro código, un 4xx sin cuerpo B2, cualquier 5xx, un timeout, un
  error de conexión o un cuerpo que no es JSON) es 503 SERVICIO_NO_DISPONIBLE, sin detalles ni
  nombres internos. El cuerpo 200 lo valida contra el DTO la ruta, que lo publica.
"""

from collections.abc import Awaitable
from typing import Any
from uuid import UUID

import httpx
from solventa_common.errors import ErrorNegocio

from bff_web.adapters.outbound.clientes import ClientesCliente, ErrorClientes

SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"
_COMUNES = {"ACTOR_REQUERIDO", "ROL_NO_AUTORIZADO", "SOLICITUD_INVALIDA"}
CODIGOS_LISTA = frozenset(_COMUNES | {"CLIENTE_NO_ENCONTRADO"})
CODIGOS_HISTORIAL = frozenset(_COMUNES | {"CONSENTIMIENTO_NO_ENCONTRADO"})


def servicio_no_disponible() -> ErrorNegocio:
    return ErrorNegocio(SERVICIO_NO_DISPONIBLE, "El servicio de clientes no está disponible", 503)


async def _proteger(llamada: Awaitable[Any], codigos: frozenset[str]) -> Any:
    try:
        return await llamada
    except ErrorClientes as error:
        codigo = error.cuerpo.get("codigo")
        if error.status < 500 and codigo in codigos:
            detalles = error.cuerpo.get("detalles")
            raise ErrorNegocio(
                str(codigo),
                str(error.cuerpo.get("mensaje") or "Solicitud rechazada"),
                error.status,
                detalles if isinstance(detalles, dict) else {},
            ) from error
        raise servicio_no_disponible() from error
    except (httpx.HTTPError, ValueError) as error:
        raise servicio_no_disponible() from error


class Consentimientos:
    def __init__(self, clientes: ClientesCliente) -> None:
        self.clientes = clientes

    async def listar(self, cliente_id: UUID, actor: str) -> Any:
        return await _proteger(self.clientes.consentimientos(cliente_id, actor), CODIGOS_LISTA)

    async def historial(self, cliente_id: UUID, consentimiento_id: UUID, actor: str) -> Any:
        return await _proteger(
            self.clientes.historial(cliente_id, consentimiento_id, actor), CODIGOS_HISTORIAL
        )
