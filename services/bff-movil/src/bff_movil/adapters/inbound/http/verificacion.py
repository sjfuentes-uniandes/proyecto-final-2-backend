"""API POST /v1/identidad/verificaciones: verificación biométrica del cliente nuevo (HU-M06)."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from solventa_common.errors import ErrorNegocio

from bff_movil.adapters.inbound.http.auth import IdentidadAutenticada, autenticar
from bff_movil.adapters.inbound.http.codigos import SERVICIO_NO_DISPONIBLE
from bff_movil.adapters.inbound.http.comun import CABECERAS, Dto, error
from bff_movil.application.verificacion.verificar_identidad import VerificarIdentidad
from bff_movil.domain.sesion.errores import ServicioNoDisponible
from bff_movil.domain.sesion.modelos import EstadoIdentidad
from bff_movil.domain.verificacion.modelos import ResultadoKyc, Verificacion

router = APIRouter(prefix="/v1/identidad/verificaciones", tags=["verificacion"])

# Tope de la selfie en base64 (~3,7 MB de imagen): por debajo del límite del API Gateway.
MAX_SELFIE = 5_000_000


class SolicitudVerificacion(Dto):
    selfie: Annotated[str, Field(min_length=1, max_length=MAX_SELFIE)]
    prueba_de_vida: dict[str, Any]


class VerificacionDto(Dto):
    cliente_id: UUID
    estado: EstadoIdentidad
    resultado: ResultadoKyc
    motivo: str | None
    verificado_en: datetime | None

    @classmethod
    def desde(cls, verificacion: Verificacion) -> "VerificacionDto":
        return cls(
            cliente_id=verificacion.cliente_id,
            estado=verificacion.estado,
            resultado=verificacion.resultado,
            motivo=verificacion.motivo,
            verificado_en=verificacion.verificado_en,
        )


@router.post(
    "",
    response_model=VerificacionDto,
    response_model_by_alias=True,
    summary="Verifica la identidad con selfie y prueba de vida",
    responses={
        200: {"headers": CABECERAS},
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        401: error(
            "NO_AUTENTICADO: falta el token, venció, tiene una firma inválida o no corresponde "
            "al titular autenticado. La app debe pedir autenticación completa"
        ),
        403: error("ROL_NO_AUTORIZADO: el token no es del pool de clientes"),
        422: error("SOLICITUD_INVALIDA: el cuerpo o un encabezado no cumple su formato"),
        503: error(
            "SERVICIO_NO_DISPONIBLE: la verificación no terminó; la app informa y permite "
            "reintentar sin crear otro cliente"
        ),
    },
)
async def verificar(
    solicitud: SolicitudVerificacion,
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> VerificacionDto:
    """Un resultado `CAPTURA_INVALIDA` es 200 con el cliente `PENDIENTE`: se repite la captura."""
    caso: VerificarIdentidad = request.app.state.verificar_identidad
    try:
        verificacion = await caso.ejecutar(
            identidad.sub, solicitud.selfie, solicitud.prueba_de_vida
        )
    except ServicioNoDisponible as exc:
        raise ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Servicio no disponible", 503) from exc
    return VerificacionDto.desde(verificacion)
