"""Registro de un cliente nuevo (HU-M06): verificación biométrica antes de tener cuenta.

Flujo: la app genera un `registroId`, verifica la identidad (ruta pública) y recibe un
`tokenRegistro`. Con él hace el SignUp de Cognito (`ClientMetadata`): el Lambda pre sign-up
rechaza el registro si no lo trae. Tras el primer ingreso, la app vincula la cuenta (ruta con
token) y desde entonces `GET /v1/sesion` encuentra al cliente.
"""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field
from solventa_common import token_registro
from solventa_common.errors import ErrorNegocio

from bff_movil.adapters.inbound.http.auth import IdentidadAutenticada, autenticar
from bff_movil.adapters.inbound.http.codigos import (
    REGISTRO_CORREO_DISTINTO,
    REGISTRO_YA_VINCULADO,
    SERVICIO_NO_DISPONIBLE,
    TOKEN_REGISTRO_INVALIDO,
)
from bff_movil.adapters.inbound.http.comun import CABECERAS, Dto, error
from bff_movil.application.registro.casos import VerificarRegistro, VincularRegistro
from bff_movil.domain.registro.modelos import (
    ClienteVinculado,
    RegistroConCorreoDistinto,
    RegistroVerificado,
    RegistroYaVinculado,
    TokenRegistroInvalido,
)
from bff_movil.domain.sesion.errores import ServicioNoDisponible
from bff_movil.domain.sesion.modelos import EstadoIdentidad
from bff_movil.domain.verificacion.modelos import ResultadoKyc

router = APIRouter(prefix="/v1/registro", tags=["registro"])
router_local = APIRouter(prefix="/v1/registro", tags=["registro"], include_in_schema=False)

# Tope de la selfie en base64 (~3,7 MB de imagen): por debajo del límite del API Gateway.
MAX_SELFIE = 5_000_000
# Validación mínima: la de verdad la hace Cognito al crear la cuenta. Sin `\s`: no significa lo
# mismo en Python que en JSON Schema, y el contrato publicado debe aceptar lo mismo que la API.
Correo = Annotated[str, Field(min_length=3, max_length=254, pattern=r"^[^@ ]+@[^@ ]+\.[^@ ]+$")]
SIN_CACHE = {"Cache-Control": "no-store"}


class SolicitudVerificacionRegistro(Dto):
    registro_id: UUID = Field(description="UUID que genera la app y reutiliza en cada reintento")
    correo: Correo
    selfie: Annotated[str, Field(min_length=1, max_length=MAX_SELFIE)]
    prueba_de_vida: dict[str, Any]


class RegistroVerificadoDto(Dto):
    cliente_id: UUID
    estado: EstadoIdentidad
    resultado: ResultadoKyc
    motivo: str | None
    verificado_en: datetime | None
    token_registro: str | None = Field(
        description="Solo con una verificación exitosa; va en ClientMetadata del SignUp"
    )
    expira_en: datetime | None

    @classmethod
    def desde(cls, registro: RegistroVerificado) -> "RegistroVerificadoDto":
        return cls(
            cliente_id=registro.cliente_id,
            estado=registro.estado,
            resultado=registro.resultado,
            motivo=registro.motivo,
            verificado_en=registro.verificado_en,
            token_registro=registro.token_registro,
            expira_en=registro.expira_en,
        )


class SolicitudVinculacion(Dto):
    registro_id: UUID
    token_registro: Annotated[str, Field(min_length=1, max_length=2048)]


class ClienteVinculadoDto(Dto):
    cliente_id: UUID
    estado: EstadoIdentidad
    verificado_en: datetime | None

    @classmethod
    def desde(cls, cliente: ClienteVinculado) -> "ClienteVinculadoDto":
        return cls(
            cliente_id=cliente.cliente_id,
            estado=cliente.estado,
            verificado_en=cliente.verificado_en,
        )


@router.post(
    "/verificaciones",
    response_model=RegistroVerificadoDto,
    response_model_by_alias=True,
    summary="Verifica la identidad antes de crear la cuenta (ruta pública)",
    responses={
        200: {"headers": CABECERAS},
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        409: error(
            "REGISTRO_CORREO_DISTINTO (la verificación va ligada a un correo) o "
            "REGISTRO_YA_VINCULADO (la cuenta de este registro ya existe)"
        ),
        422: error("SOLICITUD_INVALIDA: el cuerpo no cumple su formato"),
        503: error(
            "SERVICIO_NO_DISPONIBLE: la verificación no terminó; la app informa y permite "
            "reintentar con el mismo registroId, sin crear otro cliente"
        ),
    },
)
async def verificar(
    solicitud: SolicitudVerificacionRegistro, request: Request, response: Response
) -> RegistroVerificadoDto:
    """Pública: no lleva token y no usa `X-Authenticated-Sub`. Una captura inválida es 200 con
    `PENDIENTE` y sin `tokenRegistro`: se repite la captura."""
    caso: VerificarRegistro = request.app.state.verificar_registro
    try:
        registro = await caso.ejecutar(
            solicitud.registro_id, solicitud.correo, solicitud.selfie, solicitud.prueba_de_vida
        )
    except RegistroConCorreoDistinto as exc:
        raise ErrorNegocio(
            REGISTRO_CORREO_DISTINTO, "El registro se verificó con otro correo", 409
        ) from exc
    except RegistroYaVinculado as exc:
        raise ErrorNegocio(
            REGISTRO_YA_VINCULADO, "La cuenta de este registro ya existe", 409
        ) from exc
    except ServicioNoDisponible as exc:
        raise ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Servicio no disponible", 503) from exc
    response.headers.update(SIN_CACHE)
    return RegistroVerificadoDto.desde(registro)


@router.post(
    "/vinculacion",
    response_model=ClienteVinculadoDto,
    response_model_by_alias=True,
    summary="Asocia la cuenta recién creada con su identidad verificada",
    responses={
        200: {"headers": CABECERAS},
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        401: error(
            "NO_AUTENTICADO: falta el token, venció, tiene una firma inválida o no corresponde "
            "al titular autenticado. La app debe pedir autenticación completa"
        ),
        403: error(
            "ROL_NO_AUTORIZADO (el token no es del pool de clientes) o "
            "TOKEN_REGISTRO_INVALIDO (el tokenRegistro no sirve; detalles.motivo)"
        ),
        409: error("REGISTRO_YA_VINCULADO: ese registro o esa cuenta ya pertenecen a otro cliente"),
        422: error("SOLICITUD_INVALIDA: el cuerpo o un encabezado no cumple su formato"),
        503: error("SERVICIO_NO_DISPONIBLE: no se pudo vincular; se puede reintentar"),
    },
)
async def vincular(
    solicitud: SolicitudVinculacion,
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> ClienteVinculadoDto:
    """Idempotente: se llama una vez tras el primer ingreso y repetirla es inofensivo."""
    caso: VincularRegistro = request.app.state.vincular_registro
    try:
        cliente = await caso.ejecutar(
            solicitud.registro_id, identidad.sub, solicitud.token_registro
        )
    except TokenRegistroInvalido as exc:
        raise ErrorNegocio(
            TOKEN_REGISTRO_INVALIDO,
            "El token de registro no es válido",
            403,
            {"motivo": exc.motivo},
        ) from exc
    except RegistroYaVinculado as exc:
        raise ErrorNegocio(REGISTRO_YA_VINCULADO, "El registro ya está vinculado", 409) from exc
    except ServicioNoDisponible as exc:
        raise ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Servicio no disponible", 503) from exc
    return ClienteVinculadoDto.desde(cliente)


class SolicitudValidacion(Dto):
    token_registro: Annotated[str, Field(min_length=1, max_length=2048)]
    correo: Correo


@router_local.post("/validaciones", response_model=None)
async def validar(solicitud: SolicitudValidacion, request: Request, response: Response) -> Any:
    """Solo en local (no está en el contrato): simula el Lambda pre sign-up de Cognito.

    Aplica la misma validación que el Lambda, para probar el flujo donde no hay Cognito.
    """
    try:
        claims = token_registro.verificar(
            request.app.state.secreto_registro, solicitud.token_registro, correo=solicitud.correo
        )
    except token_registro.TokenInvalido as exc:
        raise ErrorNegocio(
            TOKEN_REGISTRO_INVALIDO,
            "El token de registro no es válido",
            403,
            {"motivo": exc.motivo},
        ) from exc
    response.headers.update(SIN_CACHE)
    return {"valido": True, "expiraEn": datetime.fromtimestamp(claims.expira_en, UTC)}
