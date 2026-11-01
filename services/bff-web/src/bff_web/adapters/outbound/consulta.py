"""Cliente HTTP de la API interna de consulta: GET /v1/polizas y GET /v1/polizas/{polizaId}/estado
(HU-W18). Solo lectura, sin reintentos.

El actor es el asesor del portal (X-Authenticated-Sub) con el rol `ASESOR`.
"""

from typing import Any
from uuid import UUID

import httpx
from solventa_common.correlation import cliente_async

from bff_web.adapters.outbound.polizas import ROL_ASESOR


class ErrorConsulta(Exception):
    """consulta respondió con un error; `cuerpo` es su JSON si lo había."""

    def __init__(self, status: int, cuerpo: dict[str, Any]) -> None:
        super().__init__(f"consulta respondió {status}")
        self.status = status
        self.cuerpo = cuerpo


class ConsultaCliente:
    def __init__(
        self, url: str, timeout_ms: int, transporte: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout_ms / 1000
        self.transporte = transporte

    async def _llamar(
        self,
        metodo: str,
        ruta: str,
        actor: str,
        rol: str,
        prefijo: str = "/v1/polizas",
        **opciones: Any,
    ) -> Any:
        encabezados = {"X-Actor-Id": actor, "X-Actor-Rol": rol}
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
            raise ErrorConsulta(respuesta.status_code, cuerpo if isinstance(cuerpo, dict) else {})
        return respuesta.json()

    async def estado(self, poliza_id: UUID, actor: str) -> Any:
        return await self._llamar("GET", f"/{poliza_id}/estado", actor, ROL_ASESOR)

    async def listado(self, parametros: dict[str, Any], actor: str) -> Any:
        return await self._llamar("GET", "", actor, ROL_ASESOR, params=parametros)
