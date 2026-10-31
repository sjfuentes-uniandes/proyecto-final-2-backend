"""Cliente HTTP de la API interna de clientes (/v1/clientes, HU-W30 y lectura de HU-W31).

Las llamadas reutilizan un `httpx.AsyncClient` por event loop y URL (conexiones persistentes): las
vistas que componen el nombre del cliente hacen varias lecturas en paralelo y abrir una conexión
por llamada las hacía llegar tarde a clientes. Con `transporte` (pruebas) se crea uno por llamada.
"""

import asyncio
import weakref
from typing import Any
from uuid import UUID

import httpx
from solventa_common.correlation import cliente_async

ROL_ASESOR = "ASESOR"

# event loop → URL base → cliente compartido. Un AsyncClient queda atado al loop donde se usa.
_compartidos: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, httpx.AsyncClient]]" = weakref.WeakKeyDictionary()


def _compartido(url: str) -> httpx.AsyncClient:
    por_url = _compartidos.setdefault(asyncio.get_running_loop(), {})
    cliente = por_url.get(url)
    if cliente is None or cliente.is_closed:
        cliente = por_url[url] = cliente_async(base_url=url)
    return cliente


async def cerrar_compartidos() -> None:
    """Cierra los clientes del loop actual (al apagar la app)."""
    for cliente in _compartidos.pop(asyncio.get_running_loop(), {}).values():
        await cliente.aclose()


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

    async def _llamar(self, metodo: str, ruta: str, actor: str, **opciones: Any) -> tuple[int, Any]:
        encabezados = {"X-Actor-Id": actor, "X-Actor-Rol": ROL_ASESOR}
        if self.transporte is None:
            respuesta = await _compartido(self.url).request(
                metodo,
                f"/v1/clientes{ruta}",
                headers=encabezados,
                timeout=self.timeout,
                **opciones,
            )
        else:
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

    async def consentimientos(self, cliente_id: UUID, actor: str) -> Any:
        return (await self._llamar("GET", f"/{cliente_id}/consentimientos", actor))[1]

    async def historial(self, cliente_id: UUID, consentimiento_id: UUID, actor: str) -> Any:
        ruta = f"/{cliente_id}/consentimientos/{consentimiento_id}/historial"
        return (await self._llamar("GET", ruta, actor))[1]
