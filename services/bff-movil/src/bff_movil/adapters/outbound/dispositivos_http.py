"""GestionDispositivos sobre la API de dispositivos del servicio `avisos` (HU-M11).

El clienteId lo resuelve el BFF desde el `sub`; el actor viaja en `X-Actor-Id` y
`X-Actor-Rol=CLIENTE`. El token del dispositivo va SOLO en el cuerpo del registro: nunca en la
URL, en logs ni en errores. Falla cerrado y nunca reintenta. No registra el cuerpo.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from solventa_common.resiliencia import (
    DependenciaNoDisponible,
    Proteccion,
    SinProteccion,
    TiempoAgotado,
)

from bff_movil.adapters.outbound.clientes_http import ROL_CLIENTE
from bff_movil.domain.dispositivos.modelos import DispositivoNoAutorizado, DispositivoNoEncontrado
from bff_movil.domain.sesion.errores import ServicioNoDisponible


class _RespuestaRegistro(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    dispositivo_id: UUID


def _codigo(respuesta: httpx.Response) -> str | None:
    try:
        codigo = respuesta.json().get("codigo")
    except (ValueError, AttributeError):
        return None
    return codigo if isinstance(codigo, str) else None


class DispositivosHttp:
    def __init__(
        self,
        cliente: httpx.AsyncClient,
        base_url: str,
        timeout_ms: int,
        proteccion: Proteccion | None = None,
    ) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._timeout_ms = timeout_ms
        self._proteccion = proteccion or SinProteccion()

    async def registrar(self, cliente_id: UUID, token: str, actor_id: str) -> UUID:
        return await self._ejecutar(
            "avisos.dispositivo.registrar",
            lambda timeout_s: self._registrar(cliente_id, token, actor_id, timeout_s),
        )

    async def dar_de_baja(self, cliente_id: UUID, dispositivo_id: UUID, actor_id: str) -> None:
        await self._ejecutar(
            "avisos.dispositivo.baja",
            lambda timeout_s: self._dar_de_baja(cliente_id, dispositivo_id, actor_id, timeout_s),
        )

    async def _ejecutar[T](self, clave: str, llamada: Callable[[float], Awaitable[T]]) -> T:
        try:
            return await self._proteccion.ejecutar(clave, "avisos", llamada, self._timeout_ms, None)
        except TiempoAgotado as error:
            raise ServicioNoDisponible("TIEMPO_AGOTADO") from error
        except DependenciaNoDisponible as error:
            raise ServicioNoDisponible("ERROR") from error

    def _cabeceras(self, actor_id: str) -> dict[str, str]:
        return {"X-Actor-Id": actor_id, "X-Actor-Rol": ROL_CLIENTE}

    async def _registrar(
        self, cliente_id: UUID, token: str, actor_id: str, timeout_s: float
    ) -> UUID:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/clientes/{cliente_id}/dispositivos",
            json={"token": token},
            headers=self._cabeceras(actor_id),
            timeout=timeout_s,
        )
        respuesta.raise_for_status()
        try:
            return _RespuestaRegistro.model_validate(respuesta.json()).dispositivo_id
        except ValueError as error:
            raise ServicioNoDisponible("ERROR") from error

    async def _dar_de_baja(
        self, cliente_id: UUID, dispositivo_id: UUID, actor_id: str, timeout_s: float
    ) -> None:
        respuesta = await self._cliente.delete(
            f"{self._base_url}/v1/clientes/{cliente_id}/dispositivos/{dispositivo_id}",
            headers=self._cabeceras(actor_id),
            timeout=timeout_s,
        )
        if respuesta.status_code == 404 and _codigo(respuesta) == "DISPOSITIVO_NO_ENCONTRADO":
            raise DispositivoNoEncontrado()
        if respuesta.status_code == 403 and _codigo(respuesta) == "DISPOSITIVO_NO_AUTORIZADO":
            raise DispositivoNoAutorizado()
        respuesta.raise_for_status()
