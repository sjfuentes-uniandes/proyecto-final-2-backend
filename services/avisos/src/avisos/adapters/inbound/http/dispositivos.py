"""API de dispositivos del cliente (HU-M11): registro (idempotente, con reasignación entre
clientes) y baja. Mismo patrón que `consulta`: el `clienteId` viaja en la ruta (lo resuelve el BFF
desde el `sub`, nunca el móvil) y el actor en `X-Actor-Id` y `X-Actor-Rol=CLIENTE`.

El token del dispositivo viaja SOLO en el cuerpo del registro: nunca en la URL, en la respuesta, en
logs ni en errores.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import ROL_NO_AUTORIZADO, ErrorNegocio

from avisos.application.dispositivos.casos import DarDeBajaDispositivo, RegistrarDispositivo
from avisos.application.dispositivos.puertos import DispositivoNoAutorizado, DispositivoNoEncontrado

ROL_CLIENTE = "CLIENTE"
SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"

router = APIRouter(prefix="/v1/clientes/{cliente_id}/dispositivos", tags=["dispositivos"])


def actor_cliente(
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_rol: Annotated[str | None, Header(alias="X-Actor-Rol")] = None,
) -> str:
    if not x_actor_id or not x_actor_rol:
        raise ErrorNegocio("ACTOR_REQUERIDO", "Faltan los encabezados del actor", 401)
    if x_actor_rol != ROL_CLIENTE:
        raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
    return x_actor_id


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SolicitudRegistro(_Dto):
    token: str = Field(min_length=1, repr=False)


class DispositivoDto(_Dto):
    dispositivo_id: UUID


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


def _errores(*extra: tuple[int, str]) -> dict[int | str, dict[str, Any]]:
    base = [
        (401, "ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
        (403, "ROL_NO_AUTORIZADO: X-Actor-Rol distinto de CLIENTE"),
        *extra,
    ]
    return {estado: _error(texto) for estado, texto in base}


def _registrar(request: Request) -> RegistrarDispositivo:
    caso = getattr(request.app.state, "registrar_dispositivo", None)
    if caso is None:
        raise ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Sin base de datos configurada", 503)
    return caso


def _dar_de_baja(request: Request) -> DarDeBajaDispositivo:
    caso = getattr(request.app.state, "dar_de_baja_dispositivo", None)
    if caso is None:
        raise ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Sin base de datos configurada", 503)
    return caso


@router.post(
    "",
    response_model=DispositivoDto,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Registra (o reasigna) el dispositivo del cliente; idempotente por el token",
    responses=_errores((503, f"{SERVICIO_NO_DISPONIBLE}")),
    dependencies=[Depends(actor_cliente)],
)
async def registrar(
    cliente_id: UUID, solicitud: SolicitudRegistro, request: Request
) -> DispositivoDto:
    caso = _registrar(request)
    dispositivo_id = await caso.ejecutar(cliente_id, solicitud.token)
    return DispositivoDto(dispositivo_id=dispositivo_id)


@router.delete(
    "/{dispositivo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Da de baja el dispositivo (queda INVALIDO; no se borra)",
    responses=_errores(
        (403, "El dispositivo es de otro cliente (sin datos del dispositivo)"),
        (404, "El dispositivo no existe"),
        (503, SERVICIO_NO_DISPONIBLE),
    ),
    dependencies=[Depends(actor_cliente)],
)
async def dar_de_baja(cliente_id: UUID, dispositivo_id: UUID, request: Request) -> None:
    caso = _dar_de_baja(request)
    try:
        await caso.ejecutar(cliente_id, dispositivo_id)
    except DispositivoNoEncontrado as error:
        raise ErrorNegocio("DISPOSITIVO_NO_ENCONTRADO", "El dispositivo no existe", 404) from error
    except DispositivoNoAutorizado as error:
        raise ErrorNegocio(
            "DISPOSITIVO_NO_AUTORIZADO", "El dispositivo no es del cliente", 403
        ) from error
