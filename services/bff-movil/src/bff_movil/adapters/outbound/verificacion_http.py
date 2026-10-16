"""ServicioVerificacion sobre POST /v1/clientes/actual/identidad/verificaciones de `clientes`."""

from datetime import datetime
from typing import Any
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
from bff_movil.domain.sesion.errores import ServicioNoDisponible
from bff_movil.domain.sesion.modelos import EstadoIdentidad
from bff_movil.domain.verificacion.modelos import ResultadoKyc, Verificacion


class _VerificacionDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    cliente_id: UUID
    estado: EstadoIdentidad
    resultado: ResultadoKyc
    motivo: str | None
    verificado_en: datetime | None


class VerificacionHttp:
    """Falla cerrado y nunca reintenta. No registra la selfie ni la prueba de vida."""

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
        self._proteccion = proteccion or SinProteccion(corte_duro_ms=timeout_ms)

    async def verificar(
        self, sub: str, selfie: str, prueba_de_vida: dict[str, Any]
    ) -> Verificacion:
        try:
            return await self._proteccion.ejecutar(
                "clientes.verificacion",
                "clientes",
                lambda timeout_s: self._enviar(sub, selfie, prueba_de_vida, timeout_s),
                self._timeout_ms,
                None,
            )
        except TiempoAgotado as error:
            raise ServicioNoDisponible("TIEMPO_AGOTADO") from error
        except DependenciaNoDisponible as error:
            raise ServicioNoDisponible("ERROR") from error

    async def _enviar(
        self, sub: str, selfie: str, prueba_de_vida: dict[str, Any], timeout_s: float
    ) -> Verificacion:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/clientes/actual/identidad/verificaciones",
            json={"selfie": selfie, "pruebaDeVida": prueba_de_vida},
            headers={"X-Actor-Id": sub, "X-Actor-Rol": ROL_CLIENTE},
            timeout=timeout_s,
        )
        # Cualquier no-200 (incluido el 503 recuperable de `clientes`) deja la verificación sin terminar.
        if respuesta.status_code != 200:
            raise ServicioNoDisponible("ERROR")
        try:
            dto = _VerificacionDto.model_validate(respuesta.json())
        except ValueError as error:
            raise ServicioNoDisponible("ERROR") from error
        return Verificacion(
            dto.cliente_id, dto.estado, dto.resultado, dto.motivo, dto.verificado_en
        )
