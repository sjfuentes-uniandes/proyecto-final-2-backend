"""Cliente HTTP de la API interna de clientes (/v1/clientes, HU-W30)."""

from typing import Any
from uuid import UUID

import httpx
from solventa_common.correlation import cliente_async

ROL_ASESOR = "ASESOR"


class ErrorClientes(Exception):
    """clientes respondió con un error; `cuerpo` es su JSON si lo había."""

    def __init__(self, status: int, cuerpo: dict[str, Any]) -> None:
        super().__init__(f"clientes respondió {status}")
        self.status = status
        self.cuerpo = cuerpo


class ClientesCliente:
    def __init__(
        self, url: str, timeout_ms: int, transporte: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout_ms / 1000
        self.transporte = transporte

    async def _llamar(
        self, metodo: str, ruta: str, actor: str, **opciones: Any
    ) -> tuple[int, dict[str, Any]]:
        encabezados = {"X-Actor-Id": actor, "X-Actor-Rol": ROL_ASESOR}
        async with cliente_async(
            base_url=self.url, timeout=self.timeout, transport=self.transporte
        ) as cliente:
            respuesta = await cliente.request(
                metodo, f"/v1/clientes{ruta}", headers=encabezados, **opciones
            )
        if respuesta.status_code >= 400:
            try:
                cuerpo = respuesta.json()
            except ValueError:
                cuerpo = {}
            raise ErrorClientes(respuesta.status_code, cuerpo if isinstance(cuerpo, dict) else {})
        return respuesta.status_code, respuesta.json()

    async def crear(self, cuerpo: dict[str, Any], actor: str) -> tuple[int, dict[str, Any]]:
        return await self._llamar("POST", "", actor, json=cuerpo)

    async def obtener(self, cliente_id: UUID, actor: str) -> dict[str, Any]:
        return (await self._llamar("GET", f"/{cliente_id}", actor))[1]

    async def verificar(self, cliente_id: UUID, actor: str) -> dict[str, Any]:
        ruta = f"/{cliente_id}/identidad/verificacion-documental"
        return (await self._llamar("POST", ruta, actor))[1]
