"""API interna de la aceptación (HU-W15, W15-B05): /v1/cotizaciones/{cotizacionId}/aceptacion.
La consume bff-web.

Actores (X-Actor-Id, X-Actor-Rol): `ASESOR` registra la aceptación en nombre del cliente
(DS2-09, canal PORTAL_ASESOR); `ASESOR` y `SUSCRIPTOR` la leen. `Idempotency-Key` obligatoria en
el POST (DS2-11). Errores en formato B2, sin detalles internos ni datos del caso.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common import correlation
from solventa_common.errors import ROL_NO_AUTORIZADO, ErrorNegocio

from polizas.adapters.inbound.http.suscripciones import (
    ErrorDto,
    actor_polizas,
    error_suscripcion,
)
from polizas.application.aceptacion.consultar import ConsultarAceptacion
from polizas.application.aceptacion.registrar import ComandoAceptacion, RegistrarAceptacion
from polizas.domain.aceptacion.errores import (
    AceptacionNoEncontrada,
    DecisionNoAprobada,
    DeclaracionesIncompletas,
    OfertaVencida,
    OfertaYaAceptada,
    TextoNoVigente,
)
from polizas.domain.aceptacion.modelos import (
    Aceptacion as AceptacionDominio,
)
from polizas.domain.aceptacion.modelos import (
    CanalAceptacion,
    Declaraciones,
    SiguientePasoContratacion,
)
from polizas.domain.suscripcion.errores import (
    ConflictoIdempotencia,
    DecisionNoEncontrada,
    DependenciaNoDisponible,
)
from polizas.domain.suscripcion.modelos import Actor, RolActor

# --- DTO (bff-web copia los mismos, con los mismos nombres) ----------------------------------


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


# --- Dominio → DTO ---------------------------------------------------------------------------


def declaraciones_dto(declaraciones: Declaraciones) -> DeclaracionesDto:
    return DeclaracionesDto(
        leido_condiciones=declaraciones.leido_condiciones,
        informacion_veraz=declaraciones.informacion_veraz,
        debito_automatico=declaraciones.debito_automatico,
        avisos_electronicos=declaraciones.avisos_electronicos,
    )


def aceptacion_dto(aceptacion: AceptacionDominio) -> Aceptacion:
    return Aceptacion(
        id=aceptacion.id,
        cotizacion_id=aceptacion.cotizacion_id,
        oferta_id=aceptacion.oferta_id,
        decision_id=aceptacion.decision_id,
        cliente_id=aceptacion.cliente_id,
        huella_calculo=aceptacion.huella_calculo,
        version_declaraciones=aceptacion.version_declaraciones,
        declaraciones=declaraciones_dto(aceptacion.declaraciones),
        canal=aceptacion.canal,
        actor_id=aceptacion.actor_id,
        aceptada_en=aceptacion.aceptada_en,
        huella_evidencia=aceptacion.huella_evidencia,
        recaudo_habilitado=True,
        siguiente_paso=SiguientePasoContratacion.FIRMA,
    )


# --- Errores ---------------------------------------------------------------------------------


def error_aceptacion(error: Exception) -> ErrorNegocio:
    """Traduce los errores de dominio a respuestas B2 estables, sin datos del caso."""
    match error:
        case TextoNoVigente():
            return ErrorNegocio(
                "TEXTO_NO_VIGENTE",
                "La versión del texto de las declaraciones no es la vigente",
                422,
            )
        case DecisionNoAprobada():
            return ErrorNegocio(
                "DECISION_NO_APROBADA", "La decisión de suscripción no permite aceptar", 409
            )
        case DeclaracionesIncompletas(campos=campos):
            return ErrorNegocio(
                "DECLARACIONES_INCOMPLETAS",
                "Faltan declaraciones obligatorias",
                422,
                {"campos": list(campos)},
            )
        case OfertaYaAceptada(aceptacion_id=aceptacion_id):
            return ErrorNegocio(
                "OFERTA_YA_ACEPTADA",
                "La oferta ya fue aceptada",
                409,
                {"aceptacionId": str(aceptacion_id)},
            )
        case AceptacionNoEncontrada():
            return ErrorNegocio("ACEPTACION_NO_ENCONTRADA", "La aceptación no existe", 404)
    # OFERTA_VENCIDA, DECISION_NO_ENCONTRADA, IDEMPOTENCIA_CONFLICTO y 503: como la suscripción.
    return error_suscripcion(error)


ERRORES_TRADUCIBLES = (
    AceptacionNoEncontrada,
    ConflictoIdempotencia,
    DecisionNoAprobada,
    DecisionNoEncontrada,
    DeclaracionesIncompletas,
    OfertaVencida,
    OfertaYaAceptada,
    TextoNoVigente,
)


# --- Rutas -----------------------------------------------------------------------------------


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/cotizaciones/{cotizacionId}/aceptacion",
    tags=["aceptaciones"],
    dependencies=[Depends(actor_polizas)],
    responses={
        401: _error("ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
        403: _error("ROL_NO_AUTORIZADO: X-Actor-Rol no permite la operación"),
    },
)

ActorPolizas = Annotated[Actor, Depends(actor_polizas)]
IdCotizacion = Annotated[UUID, Path(alias="cotizacionId")]


def _caso(request: Request, nombre: str) -> Any:
    """El caso de uso de app.state; sin los puertos configurados, 503 (no 500)."""
    caso = getattr(request.app.state, nombre, None)
    if caso is None:
        raise error_suscripcion(DependenciaNoDisponible("polizas.configuracion"))
    return caso


def _registrar(request: Request) -> RegistrarAceptacion:
    caso: RegistrarAceptacion = _caso(request, "registrar_aceptacion")
    return caso


def _consultar(request: Request) -> ConsultarAceptacion:
    caso: ConsultarAceptacion = _caso(request, "consultar_aceptacion")
    return caso


@router.post(
    "",
    status_code=201,
    response_model=Aceptacion,
    responses={
        200: {"model": Aceptacion, "description": "Misma Idempotency-Key: la aceptación existente"},
        404: _error("DECISION_NO_ENCONTRADA (la cotización no tiene esa decisión)"),
        409: _error(
            "DECISION_NO_APROBADA, OFERTA_VENCIDA (detalles.accion = NUEVA_COTIZACION) u "
            "OFERTA_YA_ACEPTADA (detalles.aceptacionId); no se registra nada"
        ),
        422: _error(
            "SOLICITUD_INVALIDA, DECLARACIONES_INCOMPLETAS (detalles.campos), TEXTO_NO_VIGENTE "
            "o IDEMPOTENCIA_CONFLICTO"
        ),
        503: _error("DEPENDENCIA_NO_DISPONIBLE"),
    },
)
async def registrar(
    cotizacion_id: IdCotizacion,
    solicitud: SolicitudAceptacion,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    respuesta: Response,
    actor: ActorPolizas,
    registrar: Annotated[RegistrarAceptacion, Depends(_registrar)],
) -> Aceptacion:
    """Registra la aceptación de la oferta aprobada y vigente, y habilita el recaudo."""
    if actor.rol is not RolActor.ASESOR:
        raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
    declaraciones = solicitud.declaraciones
    comando = ComandoAceptacion(
        cotizacion_id=cotizacion_id,
        decision_id=solicitud.decision_id,
        version_declaraciones=solicitud.version_declaraciones,
        declaraciones=Declaraciones(
            leido_condiciones=declaraciones.leido_condiciones,
            informacion_veraz=declaraciones.informacion_veraz,
            debito_automatico=declaraciones.debito_automatico,
            avisos_electronicos=declaraciones.avisos_electronicos,
        ),
        idempotency_key=idempotency_key,
        actor=actor,
        correlation_id=correlation.obtener() or "",
    )
    try:
        aceptacion, creada = await registrar.ejecutar(comando)
    except ERRORES_TRADUCIBLES as error:
        raise error_aceptacion(error) from error
    if not creada:
        respuesta.status_code = 200
    return aceptacion_dto(aceptacion)


@router.get(
    "",
    response_model=Aceptacion,
    responses={404: _error("ACEPTACION_NO_ENCONTRADA"), 422: _error("SOLICITUD_INVALIDA")},
)
async def consultar(
    cotizacion_id: IdCotizacion,
    consultar: Annotated[ConsultarAceptacion, Depends(_consultar)],
) -> Aceptacion:
    """La aceptación de la cotización, si existe (ASESOR o SUSCRIPTOR)."""
    try:
        aceptacion = await consultar.por_cotizacion(cotizacion_id)
    except AceptacionNoEncontrada as error:
        raise error_aceptacion(error) from error
    return aceptacion_dto(aceptacion)
