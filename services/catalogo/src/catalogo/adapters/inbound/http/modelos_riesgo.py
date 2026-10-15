"""API /v1/modelos-riesgo: consulta y publicación del modelo de riesgo versionado (04 §B.4.1)."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import ROL_NO_AUTORIZADO, ErrorNegocio

from catalogo.adapters.inbound.http.esquema_modelo import DefinicionModelo, a_dict
from catalogo.application.reglas.consultar_modelo import ConsultarModelo
from catalogo.application.reglas.publicar_modelo import PublicarModelo
from catalogo.domain.reglas.errores import (
    DefinicionInvalida,
    ModeloNoEncontrado,
    VersionExistente,
    VigenciaRetroactiva,
)
from catalogo.domain.reglas.modelo import ModeloPublicado

router = APIRouter(prefix="/v1/modelos-riesgo", tags=["modelos-riesgo"])

ROL_ADMIN_REGLAS = "ADMIN_REGLAS"
PATRON_VERSION = r"^\d{4}\.\d{2}\.\d+$"
PATRON_PRODUCTO = r"^[a-z0-9-]{1,64}$"

Version = Annotated[str, Field(pattern=PATRON_VERSION)]
ProductoId = Annotated[str, Field(pattern=PATRON_PRODUCTO)]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SolicitudPublicacion(_Dto):
    version: Version
    producto_id: ProductoId
    vigente_desde: AwareDatetime
    definicion: DefinicionModelo


class ModeloRiesgoRespuesta(_Dto):
    version: str
    producto_id: str
    vigente_desde: datetime
    hash: str
    definicion: DefinicionModelo

    @classmethod
    def desde(cls, modelo: ModeloPublicado) -> "ModeloRiesgoRespuesta":
        return cls(
            version=modelo.version,
            producto_id=modelo.producto_id,
            vigente_desde=modelo.vigente_desde,
            hash=modelo.hash,
            definicion=DefinicionModelo.model_validate(modelo.definicion),
        )


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


def _consultar(request: Request) -> ConsultarModelo:
    caso: ConsultarModelo = request.app.state.consultar_modelo
    return caso


def _publicar(request: Request) -> PublicarModelo:
    caso: PublicarModelo = request.app.state.publicar_modelo
    return caso


def actor_admin_reglas(
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_rol: Annotated[str | None, Header(alias="X-Actor-Rol")] = None,
) -> str:
    """El actor que publica. Se resuelve antes de validar el cuerpo: 401/403 van primero."""
    if not x_actor_id or not x_actor_rol:
        raise ErrorNegocio("ACTOR_REQUERIDO", "Faltan los encabezados del actor", 401)
    if x_actor_rol != ROL_ADMIN_REGLAS:
        raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
    return x_actor_id


def _no_encontrado(error: ModeloNoEncontrado) -> ErrorNegocio:
    return ErrorNegocio("MODELO_NO_ENCONTRADO", "No existe el modelo de riesgo solicitado", 404)


@router.get(
    "/vigente",
    response_model=ModeloRiesgoRespuesta,
    responses={
        404: _error("MODELO_NO_ENCONTRADO: no hay modelo vigente para el producto en la fecha"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def vigente(
    request: Request,
    consultar: Annotated[ConsultarModelo, Depends(_consultar)],
    producto_id: Annotated[str, Query(alias="productoId", pattern=PATRON_PRODUCTO)],
    en: Annotated[
        AwareDatetime | None,
        Query(description="Instante ISO-8601 con zona; por defecto, ahora"),
    ] = None,
) -> ModeloRiesgoRespuesta:
    """El modelo del producto con el mayor vigenteDesde ≤ en."""
    try:
        modelo = await consultar.vigente(producto_id, en or request.app.state.reloj.ahora())
    except ModeloNoEncontrado as error:
        raise _no_encontrado(error) from error
    return ModeloRiesgoRespuesta.desde(modelo)


@router.get(
    "/{version}",
    response_model=ModeloRiesgoRespuesta,
    responses={
        404: _error("MODELO_NO_ENCONTRADO"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def por_version(
    consultar: Annotated[ConsultarModelo, Depends(_consultar)],
    version: Annotated[str, Path(pattern=PATRON_VERSION)],
) -> ModeloRiesgoRespuesta:
    """Una versión publicada; las versiones no cambian nunca."""
    try:
        modelo = await consultar.por_version(version)
    except ModeloNoEncontrado as error:
        raise _no_encontrado(error) from error
    return ModeloRiesgoRespuesta.desde(modelo)


@router.post(
    "",
    status_code=201,
    response_model=ModeloRiesgoRespuesta,
    responses={
        401: _error("ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
        403: _error("ROL_NO_AUTORIZADO: X-Actor-Rol distinto de ADMIN_REGLAS"),
        409: _error("VERSION_EXISTENTE"),
        422: _error(
            "SOLICITUD_INVALIDA, DEFINICION_INVALIDA (detalles.errores, sin valores) o "
            "VIGENCIA_RETROACTIVA (vigenteDesde anterior a ahora − 5 s)"
        ),
    },
)
async def publicar(
    solicitud: SolicitudPublicacion,
    actor: Annotated[str, Depends(actor_admin_reglas)],
    publicar_modelo: Annotated[PublicarModelo, Depends(_publicar)],
) -> ModeloRiesgoRespuesta:
    """Publica una versión nueva e inmutable del modelo."""
    try:
        modelo = await publicar_modelo.ejecutar(
            version=solicitud.version,
            producto_id=solicitud.producto_id,
            vigente_desde=solicitud.vigente_desde,
            definicion=a_dict(solicitud.definicion),
            actor=actor,
        )
    except DefinicionInvalida as error:
        raise ErrorNegocio(
            "DEFINICION_INVALIDA",
            "La definición del modelo no cumple sus reglas",
            422,
            {"errores": list(error.errores)},
        ) from error
    except VigenciaRetroactiva as error:
        raise ErrorNegocio(
            "VIGENCIA_RETROACTIVA", "La vigencia no puede empezar en el pasado", 422
        ) from error
    except VersionExistente as error:
        raise ErrorNegocio("VERSION_EXISTENTE", "La versión ya está publicada", 409) from error
    return ModeloRiesgoRespuesta.desde(modelo)
