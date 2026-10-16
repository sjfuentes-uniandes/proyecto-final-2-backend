"""API GET /v1/sesion: sesión del cliente autenticado y los servicios que puede usar."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from solventa_common.errors import ErrorNegocio

from bff_movil.adapters.inbound.http.auth import IdentidadAutenticada, autenticar
from bff_movil.adapters.inbound.http.codigos import CLIENTE_NO_ENCONTRADO, SERVICIO_NO_DISPONIBLE
from bff_movil.adapters.inbound.http.comun import CABECERAS, Dto, error
from bff_movil.application.sesion.consultar_sesion import ConsultarSesion
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible
from bff_movil.domain.sesion.modelos import ServicioAutorizado, Sesion

router = APIRouter(prefix="/v1/sesion", tags=["sesion"])


class SesionDto(Dto):
    cliente_id: UUID
    servicios_autorizados: list[ServicioAutorizado]
    expira_en: datetime

    @classmethod
    def desde(cls, sesion: Sesion) -> "SesionDto":
        return cls(
            cliente_id=sesion.cliente_id,
            servicios_autorizados=list(sesion.servicios_autorizados),
            expira_en=sesion.expira_en,
        )


@router.get(
    "",
    response_model=SesionDto,
    response_model_by_alias=True,
    summary="Sesión del cliente autenticado",
    responses={
        200: {"headers": CABECERAS},
        401: error(
            "NO_AUTENTICADO: falta el token, venció, tiene una firma inválida o no corresponde "
            "al titular autenticado. La app debe pedir autenticación completa"
        ),
        403: error("ROL_NO_AUTORIZADO: el token no es del pool de clientes"),
        404: error("CLIENTE_NO_ENCONTRADO"),
        422: error("SOLICITUD_INVALIDA: un encabezado no cumple su formato"),
        503: error("SERVICIO_NO_DISPONIBLE: no se pudo verificar la identidad del cliente"),
    },
)
async def consultar_sesion(
    request: Request, identidad: Annotated[IdentidadAutenticada, Depends(autenticar)]
) -> SesionDto:
    """Revalida el token y devuelve los servicios que el cliente puede usar según su identidad."""
    caso: ConsultarSesion = request.app.state.consultar_sesion
    try:
        sesion = await caso.ejecutar(identidad.sub, identidad.expira_en)
    except ClienteNoEncontrado as error:
        raise ErrorNegocio(CLIENTE_NO_ENCONTRADO, "El cliente no existe", 404) from error
    except ServicioNoDisponible as error:
        raise ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Servicio no disponible", 503) from error
    return SesionDto.desde(sesion)
