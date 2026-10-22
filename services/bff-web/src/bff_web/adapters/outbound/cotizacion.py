"""Cliente HTTP de la API interna de cotizacion: /v1/perfiles (04 §B.4.2) y, desde HU-W03,
/v1/cotizaciones y /v1/productos/{productoId}/condiciones."""

from typing import Any
from uuid import UUID

import httpx
from solventa_common.correlation import cliente_async

ROL_ASESOR = "ASESOR"


class ErrorCotizacion(Exception):
    """cotizacion respondió con un error; `cuerpo` es su JSON si lo había."""

    def __init__(self, status: int, cuerpo: dict[str, Any]) -> None:
        super().__init__(f"cotizacion respondió {status}")
        self.status = status
        self.cuerpo = cuerpo


class CotizacionCliente:
    def __init__(
        self, url: str, timeout_ms: int, transporte: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout_ms / 1000
        self.transporte = transporte

    async def _llamar(
        self, metodo: str, ruta: str, actor: str, prefijo: str = "/v1/perfiles", **opciones: Any
    ) -> tuple[int, Any]:
        encabezados = {
            "X-Actor-Id": actor,
            "X-Actor-Rol": ROL_ASESOR,
            **opciones.pop("headers", {}),
        }
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
            raise ErrorCotizacion(respuesta.status_code, cuerpo if isinstance(cuerpo, dict) else {})
        return respuesta.status_code, respuesta.json()

    async def crear(
        self, cuerpo: dict[str, Any], actor: str, clave: UUID
    ) -> tuple[int, dict[str, Any]]:
        return await self._llamar(
            "POST", "", actor, json=cuerpo, headers={"Idempotency-Key": str(clave)}
        )

    async def recalcular(
        self, perfil_id: UUID, actor: str, clave: UUID
    ) -> tuple[int, dict[str, Any]]:
        return await self._llamar(
            "POST", f"/{perfil_id}/recalculos", actor, headers={"Idempotency-Key": str(clave)}
        )

    async def obtener(self, perfil_id: UUID, actor: str) -> dict[str, Any]:
        return (await self._llamar("GET", f"/{perfil_id}", actor))[1]

    async def listar(self, cliente_id: UUID, limite: int, actor: str) -> dict[str, Any]:
        parametros = {"clienteId": str(cliente_id), "limite": limite}
        return (await self._llamar("GET", "", actor, params=parametros))[1]

    # --- HU-W03 ---------------------------------------------------------------------------

    async def cotizar(
        self, cuerpo: dict[str, Any], actor: str, clave: UUID
    ) -> tuple[int, dict[str, Any]]:
        return await self._llamar(
            "POST",
            "",
            actor,
            prefijo="/v1/cotizaciones",
            json=cuerpo,
            headers={"Idempotency-Key": str(clave)},
        )

    async def cotizacion(self, cotizacion_id: UUID, actor: str) -> dict[str, Any]:
        return (await self._llamar("GET", f"/{cotizacion_id}", actor, prefijo="/v1/cotizaciones"))[
            1
        ]

    async def condiciones(self, producto_id: str, actor: str) -> dict[str, Any]:
        ruta = f"/{producto_id}/condiciones"
        return (await self._llamar("GET", ruta, actor, prefijo="/v1/productos"))[1]
