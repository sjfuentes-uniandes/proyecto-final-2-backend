"""ResolutorCliente sobre GET /v1/clientes/actual del servicio `clientes`."""

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
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible


class _ClienteDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    cliente_id: UUID


class ResolutorClienteHttp:
    """Relaciona el `sub` de Cognito con el clienteId. Falla cerrado y nunca reintenta."""

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

    async def resolver(self, sub: str) -> UUID:
        try:
            return await self._proteccion.ejecutar(
                "clientes.actual",
                "clientes",
                lambda timeout_s: self._consultar(sub, timeout_s),
                self._timeout_ms,
                None,
            )
        except TiempoAgotado as error:
            raise ServicioNoDisponible("TIEMPO_AGOTADO") from error
        except DependenciaNoDisponible as error:
            raise ServicioNoDisponible("ERROR") from error

    async def _consultar(self, sub: str, timeout_s: float) -> UUID:
        respuesta = await self._cliente.get(
            f"{self._base_url}/v1/clientes/actual",
            headers={"X-Actor-Id": sub, "X-Actor-Rol": ROL_CLIENTE},
            timeout=timeout_s,
        )
        if respuesta.status_code == 404:
            raise ClienteNoEncontrado()
        respuesta.raise_for_status()
        try:
            return _ClienteDto.model_validate(respuesta.json()).cliente_id
        except ValueError as error:
            raise ServicioNoDisponible("ERROR") from error
