"""ConsultaVerificacion sobre POST /v1/verificaciones del servicio `adaptador-identidad`."""

from dataclasses import dataclass
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

from clientes.domain.identidad.modelos import IdentidadNoDisponible, ResultadoKyc


class _VeredictoDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    resultado: ResultadoKyc
    motivo: str | None = None


@dataclass(frozen=True, slots=True)
class Veredicto:
    resultado: ResultadoKyc
    motivo: str | None


class AdaptadorIdentidadHttp:
    """Falla cerrado y nunca reintenta: un timeout no se repite. No registra la selfie."""

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
        self, cliente_id: UUID, selfie: str, prueba_de_vida: dict[str, Any]
    ) -> Veredicto:
        try:
            return await self._proteccion.ejecutar(
                "identidad.verificar",
                "adaptador-identidad",
                lambda timeout_s: self._llamar(cliente_id, selfie, prueba_de_vida, timeout_s),
                self._timeout_ms,
                None,
            )
        except TiempoAgotado as error:
            raise IdentidadNoDisponible("TIEMPO_AGOTADO") from error
        except DependenciaNoDisponible as error:
            raise IdentidadNoDisponible("ERROR") from error

    async def _llamar(
        self, cliente_id: UUID, selfie: str, prueba_de_vida: dict[str, Any], timeout_s: float
    ) -> Veredicto:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/verificaciones",
            json={"clienteId": str(cliente_id), "selfie": selfie, "pruebaDeVida": prueba_de_vida},
            timeout=timeout_s,
        )
        # 5xx (incluye el 503/504 del adaptador) es recuperable; un 4xx es un contrato roto.
        if respuesta.status_code in (502, 503, 504):
            raise IdentidadNoDisponible(
                "TIEMPO_AGOTADO" if respuesta.status_code == 504 else "ERROR"
            )
        if respuesta.status_code != 200:
            raise IdentidadNoDisponible("ERROR")
        try:
            veredicto = _VeredictoDto.model_validate(respuesta.json())
        except ValueError as error:
            raise IdentidadNoDisponible("ERROR") from error
        return Veredicto(veredicto.resultado, veredicto.motivo)
