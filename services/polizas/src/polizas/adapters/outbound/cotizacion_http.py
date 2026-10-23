"""InsumosOfertaPuerto sobre cotizacion (`GET /v1/cotizaciones/{id}/insumos-suscripcion`, HU-W13).

`InsumosSuscripcionDto` es la copia del consumidor del esquema `InsumosSuscripcion` de
contracts/openapi/cotizacion.yaml. Lector tolerante: ignora campos que no usa, pero una respuesta
sin los que sí usa viola el contrato y es una dependencia no disponible (falla cerrada). Propaga el
actor: polizas pide los insumos en nombre del asesor (X-Actor-Rol ASESOR).
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from polizas.domain.suscripcion.errores import CotizacionNoEncontrada, DependenciaNoDisponible
from polizas.domain.suscripcion.insumos import insumos_de
from polizas.domain.suscripcion.modelos import (
    Actor,
    EstadoIdentidad,
    InsumosSuscripcion,
    NivelRiesgo,
)

DEPENDENCIA = "cotizacion"
TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CoberturaDto(_Dto):
    codigo: str
    nombre: str
    obligatoria: bool


class PerfilInsumosDto(_Dto):
    id: UUID
    estado: str
    nivel_riesgo: NivelRiesgo | None
    confianza: TextoDecimal
    fuentes_consultadas: int
    fuentes_declaradas: int


class InsumosSuscripcionDto(_Dto):
    cotizacion_id: UUID
    oferta_id: UUID | None
    codigo: str | None
    cliente_id: UUID
    estado_identidad: EstadoIdentidad
    socio_id: str | None
    producto_id: str
    version_producto: str
    moneda: str
    periodicidad: str
    prima_total: TextoDecimal
    version_reglas_prima: str
    huella_calculo: str
    coberturas: list[CoberturaDto]
    suma_asegurada: TextoDecimal
    plazo_credito_meses: int
    edad: int
    calculado_en: datetime
    vigente_hasta: datetime
    perfil: PerfilInsumosDto | None
    coincidencias_listas_restrictivas: int | None


def a_insumos(contenido: bytes) -> InsumosSuscripcion:
    dto = InsumosSuscripcionDto.model_validate_json(contenido)
    return insumos_de(dto.model_dump(mode="json", by_alias=True))


class CotizacionHttp:
    """Implementa InsumosOfertaPuerto."""

    def __init__(self, cliente: httpx.AsyncClient, base_url: str, timeout_ms: int) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_ms / 1000

    async def insumos(self, cotizacion_id: UUID, actor: Actor) -> InsumosSuscripcion:
        try:
            respuesta = await self._cliente.get(
                f"{self._base_url}/v1/cotizaciones/{cotizacion_id}/insumos-suscripcion",
                headers={"X-Actor-Id": actor.id, "X-Actor-Rol": "ASESOR"},
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as error:
            raise DependenciaNoDisponible(DEPENDENCIA) from error
        if respuesta.status_code == 404 and _codigo(respuesta) == "COTIZACION_NO_ENCONTRADA":
            raise CotizacionNoEncontrada()
        if respuesta.status_code != 200:
            raise DependenciaNoDisponible(DEPENDENCIA)
        try:
            return a_insumos(respuesta.content)
        except (ValidationError, ValueError) as error:
            raise DependenciaNoDisponible(DEPENDENCIA) from error


def _codigo(respuesta: httpx.Response) -> str | None:
    try:
        codigo = respuesta.json().get("codigo")
    except (ValueError, AttributeError):
        return None
    return codigo if isinstance(codigo, str) else None
