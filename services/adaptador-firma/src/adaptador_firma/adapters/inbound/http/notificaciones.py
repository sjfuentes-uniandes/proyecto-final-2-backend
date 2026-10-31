"""API /v1/notificaciones: envía un aviso push al proveedor (aliado `push`, HU-M11).

Solo la llama `avisos`. El token del dispositivo viaja en el cuerpo y nunca se registra ni se
expone en respuestas, URLs o errores.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio
from solventa_common.resiliencia import DependenciaNoDisponible, TiempoAgotado

from adaptador_firma.application.notificaciones.enviar import EnviarPush
from adaptador_firma.domain.notificaciones.modelos import Push, RespuestaInvalida, ResultadoPush
from adaptador_firma.domain.notificaciones.modelos import SolicitudPush as SolicitudPushDominio

router = APIRouter(prefix="/v1/notificaciones", tags=["notificaciones"])


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SolicitudPush(_Dto):
    # El token no va en logs ni en trazas de error: repr=False lo oculta de repr() y str().
    token_dispositivo: str = Field(min_length=1, repr=False)
    tipo: str
    poliza_id: UUID
    event_id: UUID


class PushDto(_Dto):
    resultado: ResultadoPush
    envio_id: str | None = None

    @classmethod
    def de(cls, push: Push) -> "PushDto":
        return cls(resultado=push.resultado, envio_id=push.envio_id)


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


@contextmanager
def _traducir_errores_del_proveedor() -> Iterator[None]:
    try:
        yield
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


@router.post(
    "",
    response_model=PushDto,
    response_model_by_alias=True,
    responses={
        400: _error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        422: _error("SOLICITUD_INVALIDA"),
        502: _error("RESPUESTA_INVALIDA (también un 404 que no es token_no_reconocido)"),
        503: _error("FUENTE_NO_DISPONIBLE (detalles.motivo y detalles.circuito)"),
        504: _error("TIEMPO_AGOTADO"),
    },
)
async def notificar(solicitud: SolicitudPush, request: Request) -> PushDto:
    """Envía el aviso; `resultado = RECHAZADO` cuando el token del dispositivo no es válido."""
    caso: EnviarPush = request.app.state.enviar_push
    with _traducir_errores_del_proveedor():
        push = await caso.ejecutar(
            SolicitudPushDominio(
                token_dispositivo=solicitud.token_dispositivo,
                tipo=solicitud.tipo,
                poliza_id=solicitud.poliza_id,
                event_id=solicitud.event_id,
            )
        )
    return PushDto.de(push)
