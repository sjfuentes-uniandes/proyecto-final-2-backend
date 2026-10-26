"""HU-W15: aceptación de la oferta desde el portal (F01), grupo asesores (DS2-09).

Los DTO son copia de los de polizas (contracts/openapi/polizas.yaml) con los mismos nombres:
tests/contract/test_openapi.py verifica que sus esquemas sean iguales. El cuerpo de polizas se
valida contra el DTO antes de devolverlo: si no lo cumple, 503 (falla cerrada).
"""

from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Response
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.errors import RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.perfilamiento import actor
from bff_web.adapters.inbound.socios import ErrorDto
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.aceptacion import Aceptaciones
from bff_web.application.suscripcion import servicio_no_disponible
from bff_web.config import settings

# --- Enumeraciones y DTO (copia de polizas) -------------------------------------------------


class CanalAceptacion(StrEnum):
    PORTAL_ASESOR = "PORTAL_ASESOR"


class SiguientePasoContratacion(StrEnum):
    FIRMA = "FIRMA"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DeclaracionesDto(_Dto):
    """Declaraciones del tomador (F01). Obligatorias: leidoCondiciones e informacionVeraz."""

    leido_condiciones: bool
    informacion_veraz: bool
    debito_automatico: bool
    avisos_electronicos: bool


class SolicitudAceptacion(_Dto):
    decision_id: UUID
    version_declaraciones: Annotated[
        str,
        Field(
            min_length=1,
            max_length=32,
            description="Versión del texto de las declaraciones que se mostró (DS2-24)",
        ),
    ]
    declaraciones: DeclaracionesDto


class Aceptacion(_Dto):
    id: UUID
    cotizacion_id: UUID
    oferta_id: UUID | None = Field(description="Null si la decisión es anterior a HU-W04")
    decision_id: UUID
    cliente_id: UUID
    huella_calculo: str
    version_declaraciones: str
    declaraciones: DeclaracionesDto
    canal: CanalAceptacion
    actor_id: str
    aceptada_en: datetime
    huella_evidencia: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    recaudo_habilitado: bool = Field(description="Siempre true: la aceptación habilita el recaudo")
    siguiente_paso: SiguientePasoContratacion


# --- Dependencias y rutas -------------------------------------------------------------------


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/cotizaciones/{cotizacionId}/aceptacion",
    tags=["aceptaciones"],
    route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_asesores))],
    responses={
        401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
        403: _error("ROL_NO_AUTORIZADO: falta el grupo asesores o no es del back-office"),
        503: _error("SERVICIO_NO_DISPONIBLE"),
    },
)


@lru_cache
def aceptaciones() -> Aceptaciones:
    return Aceptaciones(PolizasCliente(settings.polizas_url, settings.polizas_timeout_ms))


Servicio = Annotated[Aceptaciones, Depends(aceptaciones)]
Actor = Annotated[str, Depends(actor)]
IdCotizacion = Annotated[UUID, Path(alias="cotizacionId")]
_ACEPTACION = TypeAdapter(Aceptacion)


def _en_contrato(cuerpo: Any) -> Any:
    """Devuelve el cuerpo de polizas tal cual si cumple el DTO; si no, 503 (falla cerrada)."""
    try:
        _ACEPTACION.validate_python(cuerpo)
    except ValidationError as error:
        raise servicio_no_disponible() from error
    return cuerpo


@router.post(
    "",
    response_model=Aceptacion,
    status_code=201,
    responses={
        200: {"model": Aceptacion, "description": "Misma Idempotency-Key: la aceptación existente"},
        404: _error("DECISION_NO_ENCONTRADA (la cotización no tiene esa decisión)"),
        409: _error(
            "DECISION_NO_APROBADA, OFERTA_VENCIDA (detalles.accion = NUEVA_COTIZACION) u "
            "OFERTA_YA_ACEPTADA (detalles.aceptacionId)"
        ),
        422: _error(
            "SOLICITUD_INVALIDA, DECLARACIONES_INCOMPLETAS (detalles.campos), TEXTO_NO_VIGENTE "
            "o IDEMPOTENCIA_CONFLICTO"
        ),
    },
)
async def registrar_aceptacion(
    cotizacion_id: IdCotizacion,
    solicitud: SolicitudAceptacion,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    http: Response,
    servicio: Servicio,
    asesor: Actor,
) -> Any:
    """Registra la aceptación de la oferta en nombre del cliente y habilita el recaudo."""
    estado, aceptacion = await servicio.registrar(
        cotizacion_id, solicitud.model_dump(mode="json", by_alias=True), asesor, idempotency_key
    )
    http.status_code = estado
    return _en_contrato(aceptacion)


@router.get(
    "",
    response_model=Aceptacion,
    responses={
        404: _error("ACEPTACION_NO_ENCONTRADA"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def consultar_aceptacion(
    cotizacion_id: IdCotizacion, servicio: Servicio, asesor: Actor
) -> Any:
    """La aceptación de la cotización, si existe (confirmación de F01)."""
    return _en_contrato(await servicio.consultar(cotizacion_id, asesor))
