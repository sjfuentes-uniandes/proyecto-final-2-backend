"""HU-W24: cola de descarte de los eventos paramétricos para el portal (grupo analistas-siniestros).
Reenvía a pagos-parametricos.

Falla cerrada, como HU-W18: solo pasan, con su estado y su cuerpo, los 4xx que el contrato de
pagos-parametricos promete (404 DESCARTE_NO_ENCONTRADO, 409 DESCARTE_YA_RESUELTO, 422
CORRECCION_INVALIDA o SOLICITUD_INVALIDA). Cualquier otra respuesta (un 401 o 403 de
pagos-parametricos, un 5xx, también el 503 COLA_NO_DISPONIBLE, otro código, un timeout, un error de
conexión o un cuerpo que no es JSON) es 503 SERVICIO_NO_DISPONIBLE sin detalles. El cuerpo 200 lo
valida contra el DTO la ruta.
"""

from collections.abc import Awaitable
from typing import Any

import httpx
from solventa_common.errors import ErrorNegocio
from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from bff_web.adapters.outbound.pagos_parametricos import (
    ErrorPagosParametricos,
    PagosParametricosCliente,
)

SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"
CODIGOS_DESCARTE = frozenset(
    {"DESCARTE_NO_ENCONTRADO", "DESCARTE_YA_RESUELTO", "CORRECCION_INVALIDA", "SOLICITUD_INVALIDA"}
)


def servicio_no_disponible() -> ErrorNegocio:
    return ErrorNegocio(
        SERVICIO_NO_DISPONIBLE, "El servicio de eventos paramétricos no está disponible", 503
    )


async def _proteger(llamada: Awaitable[Any]) -> Any:
    try:
        return await llamada
    except ErrorPagosParametricos as error:
        codigo = error.cuerpo.get("codigo")
        if error.status not in (404, 409, 422) or codigo not in CODIGOS_DESCARTE:
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


class Descartes:
    def __init__(self, pagos_parametricos: PagosParametricosCliente) -> None:
        self.pagos_parametricos = pagos_parametricos

    async def _medir(self, nombre: str, llamada: Awaitable[Any]) -> Any:
        with operacion(nombre) as op:
            try:
                return await _proteger(llamada)
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise

    async def listar(self, actor: str) -> Any:
        return await self._medir("descarte.listar", self.pagos_parametricos.listar(actor))

    async def detalle(self, message_id: str, actor: str) -> Any:
        return await self._medir(
            "descarte.detalle", self.pagos_parametricos.detalle(message_id, actor)
        )

    async def reprocesar(self, message_ids: list[str], actor: str) -> Any:
        return await self._medir(
            "descarte.reprocesar", self.pagos_parametricos.reprocesar(message_ids, actor)
        )

    async def corregir(self, message_id: str, cuerpo: dict[str, Any], actor: str) -> Any:
        return await self._medir(
            "descarte.corregir", self.pagos_parametricos.corregir(message_id, cuerpo, actor)
        )

    async def descartar(self, message_id: str, cuerpo: dict[str, Any], actor: str) -> Any:
        return await self._medir(
            "descarte.descartar", self.pagos_parametricos.descartar(message_id, cuerpo, actor)
        )
