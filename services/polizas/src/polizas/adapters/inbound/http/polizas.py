"""API interna de la póliza (HU-W17, 17.9; ADR 0014, decisiones 4 y 5). La consumirá bff-web
(17.11), de solo lectura.

- `GET /v1/cotizaciones/{cotizacionId}/poliza`: el estado de la emisión. Sin aceptación, 404
  ACEPTACION_NO_ENCONTRADA; con aceptación y sin póliza, 404 POLIZA_NO_ENCONTRADA (AC2). Sin la
  clave del objeto, el último fallo ni la reserva, que son internos.
- `GET /v1/polizas/{polizaId}/documento`: la URL prefirmada de la versión registrada, con el
  vencimiento configurado; 409 POLIZA_NO_ACTIVA mientras se emite.
- `POST /v1/polizas/verificaciones`: número + huella. Si no es válida, 200 con la misma forma (sin
  la firma), sea cual sea la razón: no revela si el número existe.

Mismos actores, encabezados y errores que GET …/aceptacion: `ASESOR` y `SUSCRIPTOR` leen. Errores en
formato B2.
"""

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio

from polizas.adapters.inbound.http.aceptaciones import IdCotizacion, error_aceptacion
from polizas.adapters.inbound.http.suscripciones import (
    CoberturaProductoDto,
    ErrorDto,
    TextoDecimal,
    actor_polizas,
    error_suscripcion,
)
from polizas.application.poliza.consultar import (
    ConsultarPoliza,
    DescargarDocumento,
    VerificarPoliza,
)
from polizas.domain.aceptacion.errores import AceptacionNoEncontrada
from polizas.domain.poliza.consulta import EnlaceDocumento as EnlaceDominio
from polizas.domain.poliza.consulta import Verificacion, VistaPoliza
from polizas.domain.poliza.errores import PolizaNoActiva, PolizaNoEncontrada
from polizas.domain.poliza.modelos import HUELLA, NUMERO, EstadoPoliza
from polizas.domain.suscripcion.errores import DependenciaNoDisponible
from polizas.domain.suscripcion.insumos import texto

PatronNumero = Annotated[str, Field(pattern=NUMERO.pattern)]
PatronHuella = Annotated[str, Field(pattern=HUELLA.pattern)]

# --- DTO (bff-web copiará los mismos, con los mismos nombres) --------------------------------


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ProductoPolizaDto(_Dto):
    id: str
    version: str


class RamoPolizaDto(_Dto):
    codigo: Annotated[str, Field(pattern=r"^[0-9]{3}$")]
    nombre: str


class VigenciaPolizaDto(_Dto):
    """Fechas inclusivas en America/Bogota."""

    desde: date
    hasta: date


class DocumentoPolizaDto(_Dto):
    huella: PatronHuella
    version_id: str


class FirmaPolizaDto(_Dto):
    firma_id: str
    sello_tiempo: datetime


class Poliza(_Dto):
    """La póliza de la oferta aceptada (ADR 0014). `emitidaEn`, `documento` y `firma` son nulos
    mientras no estén registrados."""

    poliza_id: UUID
    estado: EstadoPoliza
    numero: PatronNumero = Field(description="Reservado al crear la póliza: no cambia")
    producto: ProductoPolizaDto
    ramo: RamoPolizaDto
    coberturas: list[CoberturaProductoDto]
    vigencia: VigenciaPolizaDto
    suma_asegurada: TextoDecimal
    prima: TextoDecimal
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    periodicidad: str
    aceptada_en: datetime
    recaudada_en: datetime = Field(description="Cuándo dinero confirmó el recaudo")
    emitida_en: datetime | None = Field(description="Nulo mientras se emite")
    documento: DocumentoPolizaDto | None = Field(description="Nulo hasta que se sube el PDF")
    firma: FirmaPolizaDto | None = Field(description="Nula hasta que se firma su huella")


class EnlaceDocumento(_Dto):
    url: str = Field(description="URL prefirmada de la versión registrada del PDF")
    vence_en: datetime


class SolicitudVerificacion(_Dto):
    numero: PatronNumero
    huella: PatronHuella = Field(description="sha256 del PDF descargado")


class VerificacionPoliza(_Dto):
    """La misma forma sea válida o no; si no, sin la firma y sin decir por qué."""

    valido: bool
    numero: PatronNumero
    firma_id: str | None = Field(description="Solo si es válida")
    sello_tiempo: datetime | None = Field(description="Solo si es válida")


def poliza_dto(vista: VistaPoliza) -> Poliza:
    poliza, documento, firma = vista.poliza, vista.poliza.documento, vista.poliza.firma
    return Poliza(
        poliza_id=poliza.poliza_id,
        estado=poliza.estado,
        numero=poliza.numero,
        producto=ProductoPolizaDto(id=poliza.producto.id, version=poliza.producto.version),
        ramo=RamoPolizaDto(codigo=poliza.ramo.codigo, nombre=poliza.ramo.nombre),
        coberturas=[
            CoberturaProductoDto(codigo=c.codigo, nombre=c.nombre, obligatoria=c.obligatoria)
            for c in vista.coberturas
        ],
        vigencia=VigenciaPolizaDto(desde=poliza.vigencia.desde, hasta=poliza.vigencia.hasta),
        suma_asegurada=texto(vista.suma_asegurada),
        prima=texto(vista.prima),
        moneda=vista.moneda,
        periodicidad=vista.periodicidad,
        aceptada_en=vista.aceptada_en,
        recaudada_en=vista.recaudada_en,
        emitida_en=poliza.emitida_en,
        documento=None
        if documento is None
        else DocumentoPolizaDto(huella=documento.huella, version_id=documento.version_id),
        firma=None
        if firma is None
        else FirmaPolizaDto(firma_id=firma.firma_id, sello_tiempo=firma.sello_tiempo),
    )


def verificacion_dto(verificacion: Verificacion) -> VerificacionPoliza:
    firma = verificacion.firma
    return VerificacionPoliza(
        valido=verificacion.valido,
        numero=verificacion.numero,
        firma_id=None if firma is None else firma.firma_id,
        sello_tiempo=None if firma is None else firma.sello_tiempo,
    )


# --- Errores ---------------------------------------------------------------------------------


def error_poliza(error: Exception) -> ErrorNegocio:
    """Traduce los errores de dominio a respuestas B2 estables, sin datos del caso."""
    match error:
        case PolizaNoEncontrada():
            return ErrorNegocio("POLIZA_NO_ENCONTRADA", "La póliza no existe", 404)
        case PolizaNoActiva():
            return ErrorNegocio("POLIZA_NO_ACTIVA", "La póliza todavía se está emitiendo", 409)
        case AceptacionNoEncontrada():
            return error_aceptacion(error)
    # DEPENDENCIA_NO_DISPONIBLE: como la suscripción.
    return error_suscripcion(error)


ERRORES_TRADUCIBLES = (
    AceptacionNoEncontrada,
    DependenciaNoDisponible,
    PolizaNoActiva,
    PolizaNoEncontrada,
)


# --- Rutas -----------------------------------------------------------------------------------


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    tags=["polizas"],
    dependencies=[Depends(actor_polizas)],
    responses={
        401: _error("ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
        403: _error("ROL_NO_AUTORIZADO: X-Actor-Rol no permite la operación"),
    },
)

IdPoliza = Annotated[UUID, Path(alias="polizaId")]


def _caso(request: Request, nombre: str) -> Any:
    """El caso de uso de app.state; sin los puertos configurados, 503 (no 500)."""
    caso = getattr(request.app.state, nombre, None)
    if caso is None:
        raise error_suscripcion(DependenciaNoDisponible("polizas.configuracion"))
    return caso


def _consultar(request: Request) -> ConsultarPoliza:
    caso: ConsultarPoliza = _caso(request, "consultar_poliza")
    return caso


def _descargar(request: Request) -> DescargarDocumento:
    caso: DescargarDocumento = _caso(request, "descargar_documento")
    return caso


def _verificar(request: Request) -> VerificarPoliza:
    caso: VerificarPoliza = _caso(request, "verificar_poliza")
    return caso


@router.get(
    "/v1/cotizaciones/{cotizacionId}/poliza",
    response_model=Poliza,
    responses={
        404: _error(
            "ACEPTACION_NO_ENCONTRADA o POLIZA_NO_ENCONTRADA (con aceptación: el recaudo está "
            "pendiente o fue rechazado)"
        ),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def consultar_poliza(
    cotizacion_id: IdCotizacion,
    consultar: Annotated[ConsultarPoliza, Depends(_consultar)],
) -> Poliza:
    """La póliza de la oferta aceptada de la cotización (ASESOR o SUSCRIPTOR)."""
    try:
        vista = await consultar.por_cotizacion(cotizacion_id)
    except ERRORES_TRADUCIBLES as error:
        raise error_poliza(error) from error
    return poliza_dto(vista)


@router.get(
    "/v1/polizas/{polizaId}/documento",
    response_model=EnlaceDocumento,
    responses={
        404: _error("POLIZA_NO_ENCONTRADA"),
        409: _error("POLIZA_NO_ACTIVA: todavía se está emitiendo"),
        422: _error("SOLICITUD_INVALIDA"),
        503: _error("DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia = documentos)"),
    },
)
async def documento(
    poliza_id: IdPoliza,
    descargar: Annotated[DescargarDocumento, Depends(_descargar)],
) -> EnlaceDocumento:
    """URL prefirmada, de vencimiento corto, del PDF de una póliza ACTIVA (ASESOR o SUSCRIPTOR)."""
    try:
        enlace: EnlaceDominio = await descargar.ejecutar(poliza_id)
    except ERRORES_TRADUCIBLES as error:
        raise error_poliza(error) from error
    return EnlaceDocumento(url=enlace.url, vence_en=enlace.vence_en)


@router.post(
    "/v1/polizas/verificaciones",
    response_model=VerificacionPoliza,
    responses={422: _error("SOLICITUD_INVALIDA: número o huella sin el formato")},
)
async def verificar(
    solicitud: SolicitudVerificacion,
    verificar: Annotated[VerificarPoliza, Depends(_verificar)],
) -> VerificacionPoliza:
    """¿El número y la huella son los de una póliza ACTIVA firmada? (ASESOR o SUSCRIPTOR)."""
    verificacion = await verificar.ejecutar(solicitud.numero, solicitud.huella)
    return verificacion_dto(verificacion)
