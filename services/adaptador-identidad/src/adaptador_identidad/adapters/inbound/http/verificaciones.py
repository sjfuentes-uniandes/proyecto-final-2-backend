"""API POST /v1/verificaciones: verifica la identidad de un cliente con el proveedor KYC."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio
from solventa_common.resiliencia import CORTE_DURO_MS, DependenciaNoDisponible, TiempoAgotado

from adaptador_identidad.application.verificaciones.verificar_identidad import VerificarIdentidad
from adaptador_identidad.domain.verificaciones.modelos import (
    RespuestaInvalida,
    ResultadoVerificacion,
)

router = APIRouter(prefix="/v1/verificaciones", tags=["verificaciones"])

# Igual que adaptador-datos: entero opcional y no anulable (evita que Schemathesis envíe "null").
DeadlineMs = Annotated[int, Header(alias="X-Deadline-Ms", ge=1, le=CORTE_DURO_MS)]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SolicitudVerificacion(_Dto):
    cliente_id: UUID
    selfie: Annotated[str, Field(min_length=1)]
    prueba_de_vida: dict[str, Any]


class VerificacionDto(_Dto):
    resultado: ResultadoVerificacion
    motivo: str | None


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


@router.post(
    "",
    response_model=VerificacionDto,
    response_model_by_alias=True,
    responses={
        400: _error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        422: _error("SOLICITUD_INVALIDA"),
        502: _error("RESPUESTA_INVALIDA"),
        503: _error("FUENTE_NO_DISPONIBLE (detalles.motivo y detalles.circuito)"),
        504: _error("TIEMPO_AGOTADO"),
    },
)
async def verificar(
    solicitud: SolicitudVerificacion,
    request: Request,
    x_deadline_ms: DeadlineMs = None,  # type: ignore[assignment]
) -> VerificacionDto:
    """Envía la selfie y la prueba de vida al proveedor KYC y devuelve el resultado."""
    caso: VerificarIdentidad = request.app.state.verificar_identidad
    try:
        verificacion = await caso.ejecutar(
            cliente_id=solicitud.cliente_id,
            selfie=solicitud.selfie,
            prueba_de_vida=solicitud.prueba_de_vida,
            deadline_ms=x_deadline_ms,
        )
    except TiempoAgotado as error:
        raise ErrorNegocio("TIEMPO_AGOTADO", "El proveedor no respondió a tiempo", 504) from error
    except DependenciaNoDisponible as error:
        raise ErrorNegocio(
            "FUENTE_NO_DISPONIBLE",
            "El proveedor no está disponible",
            503,
            {"motivo": error.motivo, "circuito": error.circuito},
        ) from error
    except RespuestaInvalida as error:
        raise ErrorNegocio(
            "RESPUESTA_INVALIDA", "El proveedor respondió con un formato inválido", 502
        ) from error
    return VerificacionDto(resultado=verificacion.resultado, motivo=verificacion.motivo)
