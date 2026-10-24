"""Cliente HTTP de la API interna de polizas: /v1/suscripciones (HU-W13) y
/v1/cotizaciones/{cotizacionId}/aceptacion (HU-W15).

El actor es el usuario del back-office (X-Authenticated-Sub) con el rol que corresponde a su grupo:
`ASESOR` o `SUSCRIPTOR` (polizas oculta el valor de las reglas RESERVADA a quien no es
SUSCRIPTOR, DS2-07).
"""

from typing import Any
from uuid import UUID

import httpx
from solventa_common.correlation import cliente_async

ROL_ASESOR = "ASESOR"
ROL_SUSCRIPTOR = "SUSCRIPTOR"


class ErrorPolizas(Exception):
    """polizas respondió con un error; `cuerpo` es su JSON si lo había."""

    def __init__(self, status: int, cuerpo: dict[str, Any]) -> None:
        super().__init__(f"polizas respondió {status}")
        self.status = status
        self.cuerpo = cuerpo


class PolizasCliente:
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
        prefijo: str = "/v1/suscripciones",
        **opciones: Any,
    ) -> tuple[int, Any]:
        encabezados = {"X-Actor-Id": actor, "X-Actor-Rol": rol, **opciones.pop("headers", {})}
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
            raise ErrorPolizas(respuesta.status_code, cuerpo if isinstance(cuerpo, dict) else {})
        return respuesta.status_code, respuesta.json()

    async def solicitar(
        self, cuerpo: dict[str, Any], actor: str, clave: UUID
    ) -> tuple[int, dict[str, Any]]:
        return await self._llamar(
            "POST", "", actor, ROL_ASESOR, json=cuerpo, headers={"Idempotency-Key": str(clave)}
        )

    async def decision(self, decision_id: UUID, actor: str, rol: str) -> dict[str, Any]:
        return (await self._llamar("GET", f"/{decision_id}", actor, rol))[1]

    async def por_cotizacion(self, cotizacion_id: UUID, actor: str) -> dict[str, Any]:
        parametros = {"cotizacionId": str(cotizacion_id)}
        return (await self._llamar("GET", "", actor, ROL_ASESOR, params=parametros))[1]

    async def bandeja(self, parametros: dict[str, Any], actor: str) -> dict[str, Any]:
        return (await self._llamar("GET", "/bandeja", actor, ROL_SUSCRIPTOR, params=parametros))[1]

    async def resumen(self, parametros: dict[str, Any], actor: str) -> dict[str, Any]:
        return (await self._llamar("GET", "/resumen", actor, ROL_SUSCRIPTOR, params=parametros))[1]

    # --- HU-W15: aceptación de la oferta (la registra el asesor, DS2-09) ----------------------

    async def aceptar(
        self, cotizacion_id: UUID, cuerpo: dict[str, Any], actor: str, clave: UUID
    ) -> tuple[int, dict[str, Any]]:
        return await self._llamar(
            "POST",
            "",
            actor,
            ROL_ASESOR,
            prefijo=f"/v1/cotizaciones/{cotizacion_id}/aceptacion",
            json=cuerpo,
            headers={"Idempotency-Key": str(clave)},
        )

    async def aceptacion(self, cotizacion_id: UUID, actor: str) -> dict[str, Any]:
        prefijo = f"/v1/cotizaciones/{cotizacion_id}/aceptacion"
        return (await self._llamar("GET", "", actor, ROL_ASESOR, prefijo=prefijo))[1]
