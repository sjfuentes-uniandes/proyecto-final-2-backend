"""HU-W17 (17.11): la póliza de la oferta aceptada y la URL de su documento (grupo asesores).

- `GET /v1/cotizaciones/{cotizacionId}/poliza`: el estado de la emisión (EMITIENDO o ACTIVA).
- `GET /v1/polizas/{polizaId}/documento`: la URL prefirmada, de vencimiento corto, del PDF de una
  póliza ACTIVA, reenviada tal cual. Nunca va a los logs.

Los DTO son copia de los de polizas (contracts/openapi/polizas.yaml) con los mismos nombres:
tests/contract/test_openapi.py verifica que sus esquemas sean iguales. El cuerpo de polizas se
valida contra el DTO antes de devolverlo: si no lo cumple, 503 (falla cerrada).

Sin el nombre del titular ni el del canal (ADR 0014, decisión 8): la póliza de polizas no trae el
`clienteId` ni el `socioId` con que componerlos.
"""

from datetime import date, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.errors import RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.cotizaciones import CoberturaProductoDto, TextoDecimal
from bff_web.adapters.inbound.perfilamiento import actor
from bff_web.adapters.inbound.socios import ErrorDto
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.poliza import Polizas
from bff_web.application.suscripcion import servicio_no_disponible
from bff_web.config import settings

PatronNumero = Annotated[str, Field(pattern=r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$")]
PatronHuella = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

# --- Enumeraciones y DTO (copia de polizas) -------------------------------------------------


class EstadoPoliza(StrEnum):
    EMITIENDO = "EMITIENDO"
    ACTIVA = "ACTIVA"


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


# --- Dependencias y rutas -------------------------------------------------------------------


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    tags=["polizas"],
    route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_asesores))],
    responses={
        401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
        403: _error("ROL_NO_AUTORIZADO: falta el grupo asesores o no es del back-office"),
        503: _error("SERVICIO_NO_DISPONIBLE"),
    },
)


@lru_cache
def polizas() -> Polizas:
    return Polizas(PolizasCliente(settings.polizas_url, settings.polizas_timeout_ms))


Servicio = Annotated[Polizas, Depends(polizas)]
Actor = Annotated[str, Depends(actor)]
IdCotizacion = Annotated[UUID, Path(alias="cotizacionId")]
IdPoliza = Annotated[UUID, Path(alias="polizaId")]
_POLIZA = TypeAdapter(Poliza)
_ENLACE = TypeAdapter(EnlaceDocumento)


def _validar(adaptador: TypeAdapter[Any], cuerpo: dict[str, Any]) -> dict[str, Any]:
    """El cuerpo de polizas, si cumple el DTO; si no, 503."""
    try:
        adaptador.validate_python(cuerpo)
    except ValidationError as error:
        raise servicio_no_disponible() from error
    return cuerpo


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
async def consultar_poliza(cotizacion_id: IdCotizacion, servicio: Servicio, asesor: Actor) -> Any:
    """La póliza de la oferta aceptada de la cotización (solo lectura)."""
    return _validar(_POLIZA, await servicio.consultar(cotizacion_id, asesor))


@router.get(
    "/v1/polizas/{polizaId}/documento",
    response_model=EnlaceDocumento,
    responses={
        404: _error("POLIZA_NO_ENCONTRADA"),
        409: _error("POLIZA_NO_ACTIVA: todavía se está emitiendo"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def documento(poliza_id: IdPoliza, servicio: Servicio, asesor: Actor) -> Any:
    """URL prefirmada, de vencimiento corto, del PDF de una póliza ACTIVA (no va a los logs)."""
    return _validar(_ENLACE, await servicio.documento(poliza_id, asesor))
