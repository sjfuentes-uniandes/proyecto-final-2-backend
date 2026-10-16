"""API del registro previo a la cuenta de Cognito (HU-M06).

La persona verifica su identidad antes de tener cuenta, así que aún no hay `sub`: el cliente se
identifica por el `registro_id` que genera la app. Solo `bff-movil` invoca estas rutas.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field
from solventa_common.errors import ErrorNegocio

from clientes.adapters.inbound.http.comun import ERRORES_ACTOR, MAX_SELFIE, Dto, Titular, error
from clientes.adapters.inbound.http.identidad import IdentidadDto
from clientes.application.identidad.casos import VerificarRegistro, VincularRegistro
from clientes.domain.identidad.modelos import (
    EstadoIdentidad,
    IdentidadNoDisponible,
    RegistroConCorreoDistinto,
    RegistroYaVinculado,
    ResultadoKyc,
    TokenRegistroInvalido,
)

router = APIRouter(prefix="/v1/registros", tags=["registro"])

# Validación mínima: la de verdad la hace Cognito al crear la cuenta. Sin `\s`: no significa lo
# mismo en Python que en JSON Schema, y el contrato publicado debe aceptar lo mismo que la API.
Correo = Annotated[str, Field(min_length=3, max_length=254, pattern=r"^[^@ ]+@[^@ ]+\.[^@ ]+$")]


class SolicitudVerificacionRegistro(Dto):
    correo: Correo
    selfie: Annotated[str, Field(min_length=1, max_length=MAX_SELFIE)]
    prueba_de_vida: dict[str, Any]


class VerificacionRegistroDto(Dto):
    cliente_id: UUID
    estado: EstadoIdentidad
    resultado: ResultadoKyc
    motivo: str | None
    verificado_en: datetime | None
    token_registro: str | None = Field(
        description="Solo con una verificación exitosa; lo exige el SignUp de Cognito"
    )
    expira_en: datetime | None


class SolicitudVinculacion(Dto):
    token_registro: Annotated[str, Field(min_length=1, max_length=2048)]


@router.post(
    "/{registro_id}/verificaciones",
    response_model=VerificacionRegistroDto,
    response_model_by_alias=True,
    summary="Verifica la identidad antes de crear la cuenta",
    responses={
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        409: error(
            "REGISTRO_CORREO_DISTINTO (la verificación va ligada a un correo) o "
            "REGISTRO_YA_VINCULADO (la cuenta de este registro ya existe)"
        ),
        422: error("SOLICITUD_INVALIDA"),
        503: error(
            "DEPENDENCIA_NO_DISPONIBLE: la verificación no terminó; es recuperable y se puede "
            "reintentar sin crear otro cliente (detalles.motivo)"
        ),
    },
)
async def verificar(
    registro_id: UUID, solicitud: SolicitudVerificacionRegistro, request: Request
) -> VerificacionRegistroDto:
    """Una captura inválida es 200 con `PENDIENTE` y sin token: se repite la captura."""
    caso: VerificarRegistro = request.app.state.verificar_registro
    try:
        registro = await caso.ejecutar(
            registro_id, solicitud.correo, solicitud.selfie, solicitud.prueba_de_vida
        )
    except RegistroConCorreoDistinto as exc:
        raise ErrorNegocio(
            "REGISTRO_CORREO_DISTINTO", "El registro se verificó con otro correo", 409
        ) from exc
    except RegistroYaVinculado as exc:
        raise ErrorNegocio(
            "REGISTRO_YA_VINCULADO", "La cuenta de este registro ya existe", 409
        ) from exc
    except IdentidadNoDisponible as exc:
        raise ErrorNegocio(
            "DEPENDENCIA_NO_DISPONIBLE",
            "La verificación de identidad no terminó",
            503,
            {"motivo": exc.motivo},
        ) from exc
    resultado = registro.verificacion
    return VerificacionRegistroDto(
        cliente_id=resultado.cliente.id,
        estado=resultado.cliente.estado,
        resultado=resultado.resultado,
        motivo=resultado.motivo,
        verificado_en=resultado.cliente.verificado_en,
        token_registro=registro.token,
        expira_en=registro.expira_en,
    )


@router.post(
    "/{registro_id}/vinculacion",
    response_model=IdentidadDto,
    response_model_by_alias=True,
    summary="Asocia la cuenta recién creada con su identidad verificada",
    responses={
        **ERRORES_ACTOR,
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        403: error(
            "ROL_NO_AUTORIZADO (X-Actor-Rol distinto de CLIENTE) o TOKEN_REGISTRO_INVALIDO "
            "(el token no sirve; detalles.motivo)"
        ),
        409: error("REGISTRO_YA_VINCULADO: ese registro o esa cuenta ya pertenecen a otro cliente"),
        422: error("SOLICITUD_INVALIDA"),
    },
)
async def vincular(
    registro_id: UUID, solicitud: SolicitudVinculacion, request: Request, sub: Titular
) -> IdentidadDto:
    """Idempotente: repetir el vínculo del mismo `sub` devuelve el mismo cliente."""
    caso: VincularRegistro = request.app.state.vincular_registro
    try:
        cliente = await caso.ejecutar(registro_id, sub, solicitud.token_registro)
    except TokenRegistroInvalido as exc:
        raise ErrorNegocio(
            "TOKEN_REGISTRO_INVALIDO",
            "El token de registro no es válido",
            403,
            {"motivo": exc.motivo},
        ) from exc
    except RegistroYaVinculado as exc:
        raise ErrorNegocio("REGISTRO_YA_VINCULADO", "El registro ya está vinculado", 409) from exc
    return IdentidadDto.desde(cliente)
