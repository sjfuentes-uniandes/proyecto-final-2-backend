"""API de dispositivos del cliente autenticado: registro y baja del token push (HU-M11).

El titular es siempre el del token: ninguna ruta, query ni cuerpo acepta un `clienteId`. El token
del dispositivo viaja SOLO en el cuerpo del registro: nunca en la URL, en la respuesta ni en logs.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import Field
from solventa_common.errors import ErrorNegocio

from bff_movil.adapters.inbound.http.auth import IdentidadAutenticada, autenticar
from bff_movil.adapters.inbound.http.codigos import (
    CLIENTE_NO_ENCONTRADO,
    DISPOSITIVO_NO_AUTORIZADO,
    DISPOSITIVO_NO_ENCONTRADO,
    SERVICIO_NO_DISPONIBLE,
)
from bff_movil.adapters.inbound.http.comun import CABECERAS, Dto, error
from bff_movil.application.dispositivos.casos import DarDeBajaDispositivo, RegistrarDispositivo
from bff_movil.domain.dispositivos.modelos import DispositivoNoAutorizado, DispositivoNoEncontrado
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible

router = APIRouter(prefix="/v1/dispositivos", tags=["dispositivos"])


class SolicitudRegistroDispositivo(Dto):
    token: str = Field(min_length=1, repr=False)


class DispositivoDto(Dto):
    dispositivo_id: UUID


_NO_AUTENTICADO = error(
    "NO_AUTENTICADO: falta el token, venció, tiene una firma inválida o no corresponde "
    "al titular autenticado. La app debe pedir autenticación completa"
)
_ROL = error("ROL_NO_AUTORIZADO: el token no es del pool de clientes")
_NO_CLIENTE = error("CLIENTE_NO_ENCONTRADO")
_NO_DISPONIBLE = error("SERVICIO_NO_DISPONIBLE: no se pudo atender la solicitud")


def _no_disponible() -> ErrorNegocio:
    return ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Servicio no disponible", 503)


@router.post(
    "",
    response_model=DispositivoDto,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Registra el dispositivo del cliente autenticado; idempotente por el token",
    responses={
        201: {"headers": CABECERAS},
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        401: _NO_AUTENTICADO,
        403: _ROL,
        404: _NO_CLIENTE,
        422: error("SOLICITUD_INVALIDA: el cuerpo o un encabezado no cumplen su formato"),
        503: _NO_DISPONIBLE,
    },
)
async def registrar(
    solicitud: SolicitudRegistroDispositivo,
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> DispositivoDto:
    caso: RegistrarDispositivo = request.app.state.registrar_dispositivo
    try:
        dispositivo_id = await caso.ejecutar(identidad.sub, solicitud.token)
    except ClienteNoEncontrado as e:
        raise ErrorNegocio(CLIENTE_NO_ENCONTRADO, "El cliente no existe", 404) from e
    except ServicioNoDisponible as e:
        raise _no_disponible() from e
    return DispositivoDto(dispositivo_id=dispositivo_id)


@router.delete(
    "/{dispositivo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Da de baja un dispositivo del cliente autenticado",
    responses={
        204: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: error(
            "ROL_NO_AUTORIZADO: el token no es del pool de clientes, o "
            "DISPOSITIVO_NO_AUTORIZADO: el dispositivo es de otro cliente"
        ),
        404: error("CLIENTE_NO_ENCONTRADO o DISPOSITIVO_NO_ENCONTRADO"),
        422: error("SOLICITUD_INVALIDA: el identificador o un encabezado no cumplen su formato"),
        503: _NO_DISPONIBLE,
    },
)
async def dar_de_baja(
    dispositivo_id: UUID,
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> Response:
    caso: DarDeBajaDispositivo = request.app.state.dar_de_baja_dispositivo
    try:
        await caso.ejecutar(identidad.sub, dispositivo_id)
    except ClienteNoEncontrado as e:
        raise ErrorNegocio(CLIENTE_NO_ENCONTRADO, "El cliente no existe", 404) from e
    except DispositivoNoEncontrado as e:
        raise ErrorNegocio(DISPOSITIVO_NO_ENCONTRADO, "El dispositivo no existe", 404) from e
    except DispositivoNoAutorizado as e:
        raise ErrorNegocio(
            DISPOSITIVO_NO_AUTORIZADO, "El dispositivo no es del cliente", 403
        ) from e
    except ServicioNoDisponible as e:
        raise _no_disponible() from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)
