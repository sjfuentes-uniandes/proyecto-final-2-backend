"""HU-W18: estado y listado de pólizas para el portal (grupo asesores). Reenvía a consulta, de solo
lectura.

Falla cerrada: solo pasan, con su estado, el 404 POLIZA_NO_ENCONTRADA (el mismo para la póliza que no
existe y la que aún no se proyectó) y el 422 SOLICITUD_INVALIDA. Cualquier otra respuesta (un 401 o
403 de consulta, un 5xx, otro código, un timeout, un error de conexión o un cuerpo que no es JSON)
es 503 SERVICIO_NO_DISPONIBLE sin detalles. El cuerpo 200 lo valida contra el DTO la ruta.
"""

from collections.abc import Awaitable
from typing import Any
from uuid import UUID

import httpx
from solventa_common.errors import ErrorNegocio
from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.consulta import ConsultaCliente, ErrorConsulta
from bff_web.application.nombres import titular_de_cliente

SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"
CODIGOS_CONSULTA = frozenset({"POLIZA_NO_ENCONTRADA", "SOLICITUD_INVALIDA"})


def servicio_no_disponible() -> ErrorNegocio:
    return ErrorNegocio(SERVICIO_NO_DISPONIBLE, "El servicio de consulta no está disponible", 503)


async def _proteger(llamada: Awaitable[Any]) -> Any:
    try:
        return await llamada
    except ErrorConsulta as error:
        codigo = error.cuerpo.get("codigo")
        if error.status not in (404, 422) or codigo not in CODIGOS_CONSULTA:
            raise servicio_no_disponible() from error
        detalles = error.cuerpo.get("detalles")
        raise ErrorNegocio(
            str(codigo),
            str(error.cuerpo.get("mensaje") or "Solicitud rechazada"),
            error.status,
            detalles if isinstance(detalles, dict) else {},
        ) from error
    except (httpx.HTTPError, ValueError) as error:
        raise servicio_no_disponible() from error


class EstadoPolizas:
    def __init__(
        self,
        consulta: ConsultaCliente,
        clientes: ClientesCliente,
        nombre_timeout_ms: int,
    ) -> None:
        self.consulta = consulta
        self.clientes = clientes
        self._nombre_timeout_s = nombre_timeout_ms / 1000

    async def estado(self, poliza_id: UUID, actor: str) -> Any:
        """El estado de consulta más el titular de clientes; el `clienteId` no sale de aquí."""
        with operacion("poliza.estado") as op:
            try:
                cuerpo = await _proteger(self.consulta.estado(poliza_id, actor))
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
            if not isinstance(cuerpo, dict):
                return cuerpo
            cliente_id = cuerpo.pop("clienteId", None)
            titular = await titular_de_cliente(
                self.clientes, cliente_id, actor, self._nombre_timeout_s
            )
            return {**cuerpo, **titular}

    async def listar(self, parametros: dict[str, Any], actor: str) -> Any:
        with operacion("poliza.listar") as op:
            try:
                return await _proteger(self.consulta.listado(parametros, actor))
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
