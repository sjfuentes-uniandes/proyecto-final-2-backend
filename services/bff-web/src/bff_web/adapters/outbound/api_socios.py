"""Cliente de la administración de api-socios (/admin/socios) por Service Connect."""

from typing import Any

import httpx
from solventa_common.correlation import cliente_async


class ErrorApiSocios(Exception):
    """Error de api-socios con su cuerpo {codigo, mensaje, detalles} (contracts/codigos-error.md)."""

    def __init__(self, status: int, codigo: str | None, mensaje: str | None, detalles: dict[str, Any]) -> None:
        super().__init__(f"api-socios respondió {status}")
        self.status = status
        self.codigo = codigo
        self.mensaje = mensaje
        self.detalles = detalles


class ApiSocios:
    def __init__(self, url: str, transporte: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0) -> None:
        self.url = url.rstrip("/")
        self.transporte = transporte
        self.timeout = timeout

    async def _llamar(self, metodo: str, ruta: str, administrador: str | None, cuerpo: Any = None) -> Any:
        encabezados = {"X-Authenticated-Sub": administrador} if administrador else {}
        async with cliente_async(base_url=self.url, timeout=self.timeout, transport=self.transporte) as cliente:
            respuesta = await cliente.request(metodo, f"/admin/socios{ruta}", json=cuerpo, headers=encabezados)
        if respuesta.status_code >= 400:
            try:
                cuerpo = respuesta.json()
            except ValueError:
                cuerpo = {}
            cuerpo = cuerpo if isinstance(cuerpo, dict) else {}
            raise ErrorApiSocios(respuesta.status_code, cuerpo.get("codigo"), cuerpo.get("mensaje"),
                                 cuerpo.get("detalles") or {})
        return respuesta.json()

    async def listar(self, administrador: str | None) -> list[dict[str, Any]]:
        return await self._llamar("GET", "", administrador)

    async def crear(self, cuerpo: dict[str, Any], administrador: str | None) -> dict[str, Any]:
        return await self._llamar("POST", "", administrador, cuerpo)

    async def obtener(self, socio_id: str, administrador: str | None) -> dict[str, Any]:
        return await self._llamar("GET", f"/{socio_id}", administrador)

    async def desactivar(self, socio_id: str, administrador: str | None) -> dict[str, Any]:
        return await self._llamar("POST", f"/{socio_id}/desactivar", administrador)
