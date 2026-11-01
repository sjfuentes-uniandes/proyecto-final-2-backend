"""API del alta de clientes por un asesor y su verificación documental (HU-W30).

El cliente nace PENDIENTE; solo el veredicto del proveedor lo pasa a VERIFICADO o RECHAZADO.
Solo `bff-web` invoca estas rutas, con el rol ASESOR.

La lectura (`GET /v1/clientes/{clienteId}`) agrega `nombreCompleto`: `clientes` es el dueño de los
datos personales y el BFF compone con él las vistas del asesor (cotizaciones abiertas, bandeja y
decisión de suscripción), sin copiarlo en otros servicios (modelo de información, VA-005).
"""

from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import Field, field_validator
from solventa_common.errors import ErrorNegocio

from clientes.adapters.inbound.http.comun import ERRORES_ASESOR, Asesor, Dto, error
from clientes.application.identidad.casos import ConsultarCliente
from clientes.application.identidad.documental import CrearCliente, VerificarDocumento
from clientes.domain.identidad.modelos import (
    Cliente,
    ClienteNoEncontrado,
    ClienteSinDocumento,
    DatosCliente,
    EstadoIdentidad,
    IdentidadNoDisponible,
)

router = APIRouter(prefix="/v1/clientes", tags=["alta-de-clientes"])

TipoDocumento = Annotated[str, Field(pattern=r"^[A-Z]{2,3}$")]
NumeroDocumento = Annotated[str, Field(pattern=r"^[A-Za-z0-9]{3,20}$")]


class SolicitudAltaCliente(Dto):
    tipo_documento: TipoDocumento
    numero_documento: NumeroDocumento
    nombre_completo: Annotated[str, Field(min_length=1, max_length=200)]
    fecha_nacimiento: date

    @field_validator("fecha_nacimiento", mode="before")
    @classmethod
    def _solo_texto_iso(cls, valor: object) -> object:
        # Pydantic acepta un entero como marca de tiempo: aquí solo vale "AAAA-MM-DD".
        if not isinstance(valor, str):
            raise ValueError("fechaNacimiento debe ser texto AAAA-MM-DD")  # noqa: TRY004
        return valor

    @field_validator("fecha_nacimiento")
    @classmethod
    def _no_futura(cls, valor: date) -> date:
        if valor > datetime.now(UTC).date():
            raise ValueError("fechaNacimiento no puede ser futura")
        return valor


class ClienteDto(Dto):
    """Estado del cliente y traza de la verificación. No incluye PII ni el motivo del proveedor."""

    cliente_id: UUID
    estado: EstadoIdentidad
    verificado_en: datetime | None
    proveedor: str | None
    referencia: str | None

    @classmethod
    def desde(cls, cliente: Cliente) -> "ClienteDto":
        return cls(
            cliente_id=cliente.id,
            estado=cliente.estado,
            verificado_en=cliente.verificado_en,
            proveedor=cliente.proveedor,
            referencia=cliente.referencia,
        )


class ClienteLecturaDto(ClienteDto):
    """La lectura del asesor: el estado y la traza más el nombre con que se dio de alta.

    Solo el nombre: ni el documento ni la fecha de nacimiento. `null` si el cliente nació por su
    ingreso o su registro (sin alta por documento).
    """

    nombre_completo: str | None

    @classmethod
    def desde(cls, cliente: Cliente) -> "ClienteLecturaDto":
        return cls(
            **ClienteDto.desde(cliente).model_dump(),
            nombre_completo=cliente.nombre_completo,
        )


def _no_encontrado() -> ErrorNegocio:
    return ErrorNegocio("CLIENTE_NO_ENCONTRADO", "El cliente no existe", 404)


@router.post(
    "",
    status_code=201,
    response_model=ClienteDto,
    response_model_by_alias=True,
    summary="Da de alta al cliente en estado PENDIENTE",
    responses={
        **ERRORES_ASESOR,
        200: {"model": ClienteDto, "description": "El documento ya tenía cliente: se reutiliza"},
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        422: error("SOLICITUD_INVALIDA"),
    },
)
async def crear(
    solicitud: SolicitudAltaCliente, request: Request, response: Response, _asesor: Asesor
) -> ClienteDto:
    """El mismo documento devuelve el mismo cliente, sin duplicarlo ni cambiar sus datos."""
    caso: CrearCliente = request.app.state.crear_cliente
    cliente, creado = await caso.ejecutar(
        DatosCliente(
            solicitud.tipo_documento,
            solicitud.numero_documento,
            solicitud.nombre_completo,
            solicitud.fecha_nacimiento,
        )
    )
    if not creado:
        response.status_code = 200
    return ClienteDto.desde(cliente)


@router.get(
    "/{cliente_id}",
    response_model=ClienteLecturaDto,
    response_model_by_alias=True,
    summary="Estado de la identidad, traza de la verificación y nombre del cliente",
    responses={**ERRORES_ASESOR, 404: error("CLIENTE_NO_ENCONTRADO")},
)
async def obtener(cliente_id: UUID, request: Request, _asesor: Asesor) -> ClienteLecturaDto:
    consultas: ConsultarCliente = request.app.state.consultar_cliente
    try:
        return ClienteLecturaDto.desde(await consultas.identidad(cliente_id))
    except ClienteNoEncontrado as error_:
        raise _no_encontrado() from error_


@router.post(
    "/{cliente_id}/identidad/verificacion-documental",
    response_model=ClienteDto,
    response_model_by_alias=True,
    summary="Pide al proveedor que verifique la identidad del cliente por su documento",
    responses={
        **ERRORES_ASESOR,
        404: error("CLIENTE_NO_ENCONTRADO"),
        409: error("CLIENTE_SIN_DOCUMENTO: el cliente no se dio de alta con documento"),
        503: error(
            "DEPENDENCIA_NO_DISPONIBLE: el proveedor no respondió; no es un rechazo, el cliente "
            "conserva su estado y se puede reintentar (detalles.motivo y detalles.estado)"
        ),
    },
)
async def verificar(cliente_id: UUID, request: Request, _asesor: Asesor) -> ClienteDto:
    """Un cliente ya VERIFICADO responde su estado actual sin consultar de nuevo al proveedor."""
    caso: VerificarDocumento = request.app.state.verificar_documento
    consultas: ConsultarCliente = request.app.state.consultar_cliente
    try:
        resultado = await caso.ejecutar(cliente_id)
    except ClienteNoEncontrado as error_:
        raise _no_encontrado() from error_
    except ClienteSinDocumento as error_:
        raise ErrorNegocio(
            "CLIENTE_SIN_DOCUMENTO", "El cliente no tiene documento para verificar", 409
        ) from error_
    except IdentidadNoDisponible as error_:
        actual = await consultas.identidad(cliente_id)
        raise ErrorNegocio(
            "DEPENDENCIA_NO_DISPONIBLE",
            "La verificación de identidad no terminó",
            503,
            {"motivo": error_.motivo, "estado": actual.estado.value},
        ) from error_
    return ClienteDto.desde(resultado.cliente)
