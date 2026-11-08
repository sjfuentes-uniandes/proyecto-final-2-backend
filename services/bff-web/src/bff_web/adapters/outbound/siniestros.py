"""Cliente HTTP de la API interna de siniestros (/v1/siniestros, HU-W19). Sin reintentos.

Cada llamada lleva al analista (DS3-10): `X-Actor-Id` (el `sub` de Cognito), `X-Actor-Rol =
ANALISTA_SINIESTROS` y `X-Actor-Nombre`. El nombre viaja en UTF-8 (httpx solo admite ASCII en un
texto) y `siniestros` lo recupera. El aviso de un acceso denegado solo lleva `X-Actor-Id`.
"""

from typing import Any
from uuid import UUID

import httpx
from solventa_common.correlation import cliente_async

ROL_ANALISTA_SINIESTROS = "ANALISTA_SINIESTROS"


def _en_bytes(encabezados: dict[str, str | bytes]) -> list[tuple[bytes, bytes]]:
    return [
        (nombre.encode("ascii"), valor if isinstance(valor, bytes) else valor.encode("utf-8"))
        for nombre, valor in encabezados.items()
    ]


class ErrorSiniestros(Exception):
    """siniestros respondió con un error; `cuerpo` es su JSON si lo había."""

    def __init__(self, status: int, cuerpo: dict[str, Any]) -> None:
        super().__init__(f"siniestros respondió {status}")
        self.status = status
        self.cuerpo = cuerpo


class SiniestrosCliente:
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
        encabezados: dict[str, str | bytes],
        timeout: float | None = None,
        **opciones: Any,
    ) -> httpx.Response:
        async with cliente_async(
            base_url=self.url, timeout=timeout or self.timeout, transport=self.transporte
        ) as cliente:
            return await cliente.request(
                metodo, f"/v1/siniestros{ruta}", headers=_en_bytes(encabezados), **opciones
            )

    @staticmethod
    def _actor(actor: str, nombre: str) -> dict[str, str | bytes]:
        return {
            "X-Actor-Id": actor,
            "X-Actor-Rol": ROL_ANALISTA_SINIESTROS,
            "X-Actor-Nombre": nombre.encode("utf-8"),
        }

    async def _json(
        self,
        metodo: str,
        ruta: str,
        actor: str,
        nombre: str,
        extra: dict[str, str] | None = None,
        **opciones: Any,
    ) -> Any:
        respuesta = await self._llamar(
            metodo, ruta, {**self._actor(actor, nombre), **(extra or {})}, **opciones
        )
        if respuesta.status_code >= 400:
            try:
                cuerpo = respuesta.json()
            except ValueError:
                cuerpo = {}
            raise ErrorSiniestros(respuesta.status_code, cuerpo if isinstance(cuerpo, dict) else {})
        return respuesta.json()

    async def bandeja(self, parametros: dict[str, Any], actor: str, nombre: str) -> Any:
        return await self._json("GET", "", actor, nombre, params=parametros)

    async def resumen(self, parametros: dict[str, Any], actor: str, nombre: str) -> Any:
        return await self._json("GET", "/resumen", actor, nombre, params=parametros)

    async def tomar_siguiente(self, clave: UUID, actor: str, nombre: str) -> Any:
        return await self._json(
            "POST", "/siguiente", actor, nombre, extra={"Idempotency-Key": str(clave)}
        )

    async def detalle(self, siniestro_id: UUID, actor: str, nombre: str) -> Any:
        return await self._json("GET", f"/{siniestro_id}", actor, nombre)

    async def enlace(self, siniestro_id: UUID, evidencia_id: UUID, actor: str, nombre: str) -> Any:
        return await self._json(
            "POST", f"/{siniestro_id}/evidencias/{evidencia_id}/enlace", actor, nombre
        )

    async def acceso_denegado(
        self, actor: str, recurso: str, recurso_id: str | None, motivo: str, timeout_s: float
    ) -> None:
        """Avisa que `bff-web` rechazó a un usuario (DS3-09). Un 4xx o 5xx es un fallo."""
        respuesta = await self._llamar(
            "POST",
            "/accesos-denegados",
            {"X-Actor-Id": actor},
            timeout=timeout_s,
            json={
                "actorId": actor,
                "recurso": recurso,
                "recursoId": recurso_id,
                "motivo": motivo,
            },
        )
        if respuesta.status_code != 202:
            raise ErrorSiniestros(respuesta.status_code, {})
