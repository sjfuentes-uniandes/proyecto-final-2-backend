"""Cliente HTTP de la API interna de pagos-parametricos: la cola de descarte de
`parametricos-eventos` (HU-W24; H10 y H11). Sin reintentos: las acciones son idempotentes por
`messageId` en pagos-parametricos, pero el BFF no las repite por su cuenta.

Llamada `bff-web → pagos-parametricos`: decisión B1 de HU-W24, sin flecha en los diagramas (ver
docs/hu-w24-particiones.md). El actor es el analista del portal (X-Authenticated-Sub) con el rol
`ANALISTA_SINIESTROS`.
"""

from typing import Any

import httpx
from solventa_common.correlation import cliente_async

ROL_ANALISTA = "ANALISTA_SINIESTROS"
PREFIJO = "/v1/parametricos/descartes"
# HU-W22: el seguimiento de H09 (resumen y orígenes recientes) cuelga de `/v1/parametricos`.
PREFIJO_SEGUIMIENTO = "/v1/parametricos"


class ErrorPagosParametricos(Exception):
    """pagos-parametricos respondió con un error; `cuerpo` es su JSON si lo había."""

    def __init__(self, status: int, cuerpo: dict[str, Any]) -> None:
        super().__init__(f"pagos-parametricos respondió {status}")
        self.status = status
        self.cuerpo = cuerpo


class PagosParametricosCliente:
    def __init__(
        self, url: str, timeout_ms: int, transporte: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout_ms / 1000
        self.transporte = transporte

    async def _llamar(
        self, metodo: str, ruta: str, actor: str, prefijo: str = PREFIJO, **opciones: Any
    ) -> Any:
        encabezados = {"X-Actor-Id": actor, "X-Actor-Rol": ROL_ANALISTA}
        async with cliente_async(
            base_url=self.url, timeout=self.timeout, transport=self.transporte
        ) as cliente:
            respuesta = await cliente.request(
                metodo, f"{prefijo}{ruta}", headers=encabezados, **opciones
            )
        if respuesta.status_code >= 400:
            try:
                cuerpo = respuesta.json()
            except ValueError:
                cuerpo = {}
            raise ErrorPagosParametricos(
                respuesta.status_code, cuerpo if isinstance(cuerpo, dict) else {}
            )
        return respuesta.json()

    async def listar(self, actor: str) -> Any:
        return await self._llamar("GET", "", actor)

    async def detalle(self, message_id: str, actor: str) -> Any:
        return await self._llamar("GET", f"/{message_id}", actor)

    async def reprocesar(self, message_ids: list[str], actor: str) -> Any:
        return await self._llamar("POST", "/reproceso", actor, json={"messageIds": message_ids})

    async def corregir(self, message_id: str, cuerpo: dict[str, Any], actor: str) -> Any:
        return await self._llamar("POST", f"/{message_id}/correccion", actor, json=cuerpo)

    async def descartar(self, message_id: str, cuerpo: dict[str, Any], actor: str) -> Any:
        return await self._llamar("POST", f"/{message_id}/descarte", actor, json=cuerpo)

    async def resumen(self, evento_origen: str | None, actor: str) -> Any:
        """`GET /v1/parametricos/resumen` (HU-W22, H09). Sin `eventoOrigen`, el más reciente."""
        parametros = {"eventoOrigen": evento_origen} if evento_origen else None
        return await self._llamar(
            "GET", "/resumen", actor, prefijo=PREFIJO_SEGUIMIENTO, params=parametros
        )

    async def origenes(self, limite: int, actor: str) -> Any:
        """`GET /v1/parametricos/eventos-origen` (HU-W22): los orígenes recientes del selector."""
        return await self._llamar(
            "GET", "/eventos-origen", actor, prefijo=PREFIJO_SEGUIMIENTO, params={"limite": limite}
        )
