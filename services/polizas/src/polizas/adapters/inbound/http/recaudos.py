"""API interna del estado del recaudo (HU-W16, 16.13; ADR 0013, decisión 9):
GET /v1/cotizaciones/{cotizacionId}/recaudo. La consumirá bff-web (16.14), de solo lectura.

Mismos actores, encabezados y errores que GET …/aceptacion: `ASESOR` y `SUSCRIPTOR` leen.
Sin aceptación, 404 ACEPTACION_NO_ENCONTRADA. Con aceptación y sin estado final de dinero,
PENDIENTE con la prima de la instantánea de la decisión. Errores en formato B2.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from polizas.adapters.inbound.http.aceptaciones import error_aceptacion
from polizas.adapters.inbound.http.suscripciones import (
    ErrorDto,
    TextoDecimal,
    actor_polizas,
    error_suscripcion,
)
from polizas.application.recaudo.consultar import ConsultarRecaudo
from polizas.domain.aceptacion.errores import AceptacionNoEncontrada
from polizas.domain.recaudo.modelos import EstadoRecaudo as EstadoRecaudoDominio
from polizas.domain.recaudo.modelos import SituacionRecaudo
from polizas.domain.suscripcion.errores import DependenciaNoDisponible
from polizas.domain.suscripcion.insumos import texto

# --- DTO (bff-web copiará el mismo, con el mismo nombre) ------------------------------------


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


def estado_recaudo_dto(estado: EstadoRecaudoDominio) -> EstadoRecaudo:
    return EstadoRecaudo(
        estado=estado.estado,
        clave_recaudo=estado.clave_recaudo,
        monto=texto(estado.monto),
        moneda=estado.moneda,
        transaccion_id=estado.transaccion_id,
        motivo=estado.motivo,
        actualizado_en=estado.actualizado_en,
    )


# --- Rutas -----------------------------------------------------------------------------------


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/cotizaciones/{cotizacionId}/recaudo",
    tags=["recaudos"],
    dependencies=[Depends(actor_polizas)],
    responses={
        401: _error("ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
        403: _error("ROL_NO_AUTORIZADO: X-Actor-Rol no permite la operación"),
    },
)

IdCotizacion = Annotated[UUID, Path(alias="cotizacionId")]


def _consultar(request: Request) -> ConsultarRecaudo:
    """El caso de uso de app.state; sin los puertos configurados, 503 (no 500)."""
    caso: ConsultarRecaudo | None = getattr(request.app.state, "consultar_recaudo", None)
    if caso is None:
        raise error_suscripcion(DependenciaNoDisponible("polizas.configuracion"))
    return caso


@router.get(
    "",
    response_model=EstadoRecaudo,
    responses={404: _error("ACEPTACION_NO_ENCONTRADA"), 422: _error("SOLICITUD_INVALIDA")},
)
async def consultar(
    cotizacion_id: IdCotizacion,
    consultar: Annotated[ConsultarRecaudo, Depends(_consultar)],
) -> EstadoRecaudo:
    """El estado del recaudo de la prima aceptada (ASESOR o SUSCRIPTOR)."""
    try:
        estado = await consultar.por_cotizacion(cotizacion_id)
    except AceptacionNoEncontrada as error:
        raise error_aceptacion(error) from error
    except DependenciaNoDisponible as error:
        raise error_suscripcion(error) from error
    return estado_recaudo_dto(estado)
