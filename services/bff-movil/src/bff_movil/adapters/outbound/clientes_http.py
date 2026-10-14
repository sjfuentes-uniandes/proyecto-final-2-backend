"""ConsultaIdentidad sobre GET /v1/clientes/{clienteId}/identidad del servicio `clientes`."""

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

from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible
from bff_movil.domain.sesion.modelos import EstadoIdentidad

ROL_CLIENTE = "CLIENTE"


class _IdentidadDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    cliente_id: UUID
    estado: EstadoIdentidad


class ClientesHttp:
    """Falla cerrado y nunca reintenta: un timeout no se repite (C5). No registra el cuerpo."""

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

    async def obtener(self, cliente_id: UUID, actor_id: str) -> EstadoIdentidad:
        try:
            return await self._proteccion.ejecutar(
                "clientes.identidad",
                "clientes",
                lambda timeout_s: self._consultar(cliente_id, actor_id, timeout_s),
                self._timeout_ms,
                None,
            )
        except TiempoAgotado as error:
            raise ServicioNoDisponible("TIEMPO_AGOTADO") from error
        except DependenciaNoDisponible as error:
            raise ServicioNoDisponible("ERROR") from error

    async def _consultar(
        self, cliente_id: UUID, actor_id: str, timeout_s: float
    ) -> EstadoIdentidad:
        respuesta = await self._cliente.get(
            f"{self._base_url}/v1/clientes/{cliente_id}/identidad",
            headers={"X-Actor-Id": actor_id, "X-Actor-Rol": ROL_CLIENTE},
            timeout=timeout_s,
        )
        if respuesta.status_code == 404:
            raise ClienteNoEncontrado()
        respuesta.raise_for_status()
        try:
            identidad = _IdentidadDto.model_validate(respuesta.json())
        except ValueError as error:
            raise ServicioNoDisponible("ERROR") from error
        if identidad.cliente_id != cliente_id:
            raise ServicioNoDisponible("ERROR")
        return identidad.estado
