"""HU-W16 (16.14): estado del recaudo de la prima aceptada, paso 3 del portal (grupo asesores).

El DTO es copia del de polizas (contracts/openapi/polizas.yaml) con los mismos nombres:
tests/contract/test_openapi.py verifica que sus esquemas sean iguales. El cuerpo de polizas se
valida contra el DTO antes de devolverlo: si no lo cumple, 503 (falla cerrada).
"""

from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.errors import RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.cotizaciones import TextoDecimal
from bff_web.adapters.inbound.perfilamiento import actor
from bff_web.adapters.inbound.socios import ErrorDto
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.recaudo import Recaudos
from bff_web.application.suscripcion import servicio_no_disponible
from bff_web.config import settings

# --- Enumeraciones y DTO (copia de polizas) -------------------------------------------------


class SituacionRecaudo(StrEnum):
    PENDIENTE = "PENDIENTE"
    CONFIRMADO = "CONFIRMADO"
    RECHAZADO = "RECHAZADO"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class EstadoRecaudo(_Dto):
    """El estado del primer cobro de la prima de la aceptación (ADR 0013, decisión 9)."""

    estado: SituacionRecaudo = Field(
        description="PENDIENTE mientras dinero no publica un estado final"
    )
    clave_recaudo: UUID = Field(description="El id de la aceptación")
    monto: TextoDecimal = Field(
        description="Lo cobrado si está CONFIRMADO; si no, la prima de la oferta aceptada"
    )
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    transaccion_id: str | None = Field(description="Solo en CONFIRMADO")
    motivo: str | None = Field(description="Código del rechazo; solo en RECHAZADO")
    actualizado_en: datetime = Field(
        description="Cuándo dinero resolvió el cobro; si está PENDIENTE, cuándo se aceptó"
    )


# --- Dependencias y rutas -------------------------------------------------------------------


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/cotizaciones/{cotizacionId}/recaudo",
    tags=["recaudos"],
    route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_asesores))],
    responses={
        401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
        403: _error("ROL_NO_AUTORIZADO: falta el grupo asesores o no es del back-office"),
        503: _error("SERVICIO_NO_DISPONIBLE"),
    },
)


@lru_cache
def recaudos() -> Recaudos:
    return Recaudos(PolizasCliente(settings.polizas_url, settings.polizas_timeout_ms))


Servicio = Annotated[Recaudos, Depends(recaudos)]
Actor = Annotated[str, Depends(actor)]
IdCotizacion = Annotated[UUID, Path(alias="cotizacionId")]
_ESTADO_RECAUDO = TypeAdapter(EstadoRecaudo)


@router.get(
    "",
    response_model=EstadoRecaudo,
    responses={
        404: _error("ACEPTACION_NO_ENCONTRADA"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def consultar_recaudo(cotizacion_id: IdCotizacion, servicio: Servicio, asesor: Actor) -> Any:
    """El estado del primer cobro de la prima aceptada (paso 3 del portal, solo lectura)."""
    estado = await servicio.consultar(cotizacion_id, asesor)
    try:
        _ESTADO_RECAUDO.validate_python(estado)
    except ValidationError as error:
        raise servicio_no_disponible() from error
    return estado
