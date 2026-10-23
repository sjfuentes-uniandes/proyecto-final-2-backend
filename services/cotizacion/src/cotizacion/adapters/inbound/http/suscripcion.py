"""API interna de los insumos de la oferta para la suscripción (HU-W13, W13-B12).

La consume `polizas` (`GET /v1/cotizaciones/{cotizacionId}/insumos-suscripcion`) en nombre del
asesor (X-Actor-Rol ASESOR); un socio no la usa. Montos y confianza como texto decimal (DS2-16).
"""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from cotizacion.adapters.inbound.http.cotizaciones import (
    DESCRIPCION_VIGENTE_HASTA,
    ActorCotizacion,
    CoberturaProductoDto,
    TextoDecimal,
    actor_cotizacion,
    error_cotizacion,
)
from cotizacion.adapters.inbound.http.perfilamiento import ErrorDto
from cotizacion.application.suscripcion.insumos import (
    ConsultarInsumos,
    Insumos,
    coincidencias_listas,
    fuentes_consultadas,
)
from cotizacion.domain.cotizacion.errores import CotizacionNoEncontrada, RolNoPermitido
from cotizacion.domain.cotizacion.prima import texto
from cotizacion.domain.perfilamiento.errores import DependenciaNoDisponible
from cotizacion.domain.perfilamiento.modelos import EstadoIdentidad, EstadoPerfil, NivelRiesgo


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/cotizaciones",
    tags=["suscripcion"],
    dependencies=[Depends(actor_cotizacion)],
    responses={
        401: _error("ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
        403: _error("ROL_NO_AUTORIZADO: X-Actor-Rol distinto de ASESOR"),
    },
)


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PerfilInsumosDto(_Dto):
    """Lo mínimo del perfil que evalúan las reglas: sin factores ni datos de las fuentes."""

    id: UUID
    estado: EstadoPerfil
    nivel_riesgo: NivelRiesgo | None
    confianza: TextoDecimal
    fuentes_consultadas: int
    fuentes_declaradas: int


class InsumosSuscripcion(_Dto):
    """Resumen de la cotización para la suscripción (HU-W13). Sin fecha de nacimiento: `edad` es
    la cumplida en la fecha de la cotización. Una cotización vencida también se devuelve."""

    cotizacion_id: UUID
    oferta_id: UUID | None = Field(description="Identificador de la oferta; null hasta HU-W04")
    codigo: str | None = Field(description="Código legible de la oferta; null hasta HU-W04")
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
    coberturas: list[CoberturaProductoDto]
    suma_asegurada: TextoDecimal
    plazo_credito_meses: int
    edad: int
    calculado_en: datetime
    vigente_hasta: datetime = Field(description=DESCRIPCION_VIGENTE_HASTA)
    perfil: PerfilInsumosDto | None = Field(description="Null si la cotización no usó perfil")
    coincidencias_listas_restrictivas: int | None = Field(
        description="De DA_REGISTROS_PUBLICOS en el perfil; null si no se consultó"
    )


def insumos_de(insumos: Insumos) -> InsumosSuscripcion:
    cotizacion, perfil = insumos.cotizacion, insumos.perfil
    entradas = cotizacion.entradas
    return InsumosSuscripcion(
        cotizacion_id=cotizacion.id,
        oferta_id=None,
        codigo=None,
        cliente_id=cotizacion.cliente_id,
        estado_identidad=insumos.estado_identidad,
        socio_id=cotizacion.socio_id,
        producto_id=cotizacion.producto_id,
        version_producto=cotizacion.version_producto,
        moneda=cotizacion.moneda,
        periodicidad=cotizacion.periodicidad,
        prima_total=texto(cotizacion.desglose.prima_total),
        version_reglas_prima=cotizacion.version_reglas,
        huella_calculo=cotizacion.huella_calculo,
        coberturas=[
            CoberturaProductoDto(codigo=c.codigo, nombre=c.nombre, obligatoria=c.obligatoria)
            for c in cotizacion.coberturas
        ],
        suma_asegurada=texto(entradas.suma_asegurada),
        plazo_credito_meses=entradas.plazo_credito_meses,
        edad=entradas.edad,
        calculado_en=cotizacion.calculado_en.astimezone(UTC),
        vigente_hasta=insumos.vigente_hasta.astimezone(UTC),
        perfil=None
        if perfil is None
        else PerfilInsumosDto(
            id=perfil.id,
            estado=perfil.resultado.estado,
            nivel_riesgo=perfil.resultado.nivel,
            confianza=texto(perfil.resultado.confianza),
            fuentes_consultadas=fuentes_consultadas(perfil),
            fuentes_declaradas=len(perfil.fuentes),
        ),
        coincidencias_listas_restrictivas=coincidencias_listas(perfil),
    )


def _consultar(request: Request) -> ConsultarInsumos:
    caso = getattr(request.app.state, "consultar_insumos", None)
    if caso is None:
        raise error_cotizacion(DependenciaNoDisponible("cotizacion.configuracion"))
    consultar: ConsultarInsumos = caso
    return consultar


@router.get(
    "/{cotizacionId}/insumos-suscripcion",
    response_model=InsumosSuscripcion,
    responses={
        404: _error("COTIZACION_NO_ENCONTRADA"),
        422: _error("SOLICITUD_INVALIDA"),
        503: _error("DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia: clientes.identidad)"),
    },
)
async def insumos_suscripcion(
    cotizacion_id: Annotated[UUID, Path(alias="cotizacionId")],
    actor: ActorCotizacion,
    consultar: Annotated[ConsultarInsumos, Depends(_consultar)],
) -> InsumosSuscripcion:
    """Insumos de la oferta para el motor de suscripción de polizas (uso interno, ASESOR)."""
    try:
        insumos = await consultar.ejecutar(cotizacion_id, actor)
    except (CotizacionNoEncontrada, RolNoPermitido, DependenciaNoDisponible) as error:
        raise error_cotizacion(error) from error
    return insumos_de(insumos)
