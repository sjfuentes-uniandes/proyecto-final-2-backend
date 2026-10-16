"""API de la identidad del cliente (HU-M06)."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field
from solventa_common.errors import ErrorNegocio

from clientes.adapters.inbound.http.comun import (
    ERRORES_ACTOR,
    MAX_SELFIE,
    Dto,
    Titular,
    error,
)
from clientes.application.identidad.casos import ConsultarCliente, VerificarIdentidad
from clientes.domain.identidad.modelos import (
    Cliente,
    ClienteNoEncontrado,
    EstadoIdentidad,
    IdentidadNoDisponible,
    ResultadoKyc,
)

router = APIRouter(prefix="/v1/clientes", tags=["identidad"])


class IdentidadDto(Dto):
    cliente_id: UUID
    estado: EstadoIdentidad
    verificado_en: datetime | None

    @classmethod
    def desde(cls, cliente: Cliente) -> "IdentidadDto":
        return cls(
            cliente_id=cliente.id, estado=cliente.estado, verificado_en=cliente.verificado_en
        )


class SolicitudVerificacion(Dto):
    selfie: Annotated[str, Field(min_length=1, max_length=MAX_SELFIE)]
    prueba_de_vida: dict[str, Any]


class VerificacionDto(Dto):
    cliente_id: UUID
    estado: EstadoIdentidad
    resultado: ResultadoKyc
    motivo: str | None
    verificado_en: datetime | None


@router.get(
    "/actual",
    response_model=IdentidadDto,
    response_model_by_alias=True,
    summary="Cliente del titular autenticado",
    responses={
        **ERRORES_ACTOR,
        404: error("CLIENTE_NO_ENCONTRADO: el titular aún no inició su verificación"),
    },
)
async def cliente_actual(request: Request, sub: Titular) -> IdentidadDto:
    """Resuelve el cliente del `sub` del titular, sin crearlo."""
    consultas: ConsultarCliente = request.app.state.consultar_cliente
    try:
        return IdentidadDto.desde(await consultas.por_sub(sub))
    except ClienteNoEncontrado as error:
        raise ErrorNegocio("CLIENTE_NO_ENCONTRADO", "El cliente no existe", 404) from error


@router.get(
    "/{cliente_id}/identidad",
    response_model=IdentidadDto,
    response_model_by_alias=True,
    summary="Estado de la verificación de identidad del cliente",
    responses={404: error("CLIENTE_NO_ENCONTRADO")},
)
async def identidad(cliente_id: UUID, request: Request) -> IdentidadDto:
    consultas: ConsultarCliente = request.app.state.consultar_cliente
    try:
        return IdentidadDto.desde(await consultas.identidad(cliente_id))
    except ClienteNoEncontrado as error:
        raise ErrorNegocio("CLIENTE_NO_ENCONTRADO", "El cliente no existe", 404) from error


@router.post(
    "/actual/identidad/verificaciones",
    response_model=VerificacionDto,
    response_model_by_alias=True,
    summary="Verifica la identidad con selfie y prueba de vida",
    responses={
        **ERRORES_ACTOR,
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        422: error("SOLICITUD_INVALIDA"),
        503: error(
            "DEPENDENCIA_NO_DISPONIBLE: la verificación no terminó; es recuperable y se puede "
            "reintentar sin crear otro cliente (detalles.motivo)"
        ),
    },
)
async def verificar(
    solicitud: SolicitudVerificacion, request: Request, sub: Titular
) -> VerificacionDto:
    """Un `CAPTURA_INVALIDA` responde 200 con el cliente aún pendiente: no es un rechazo."""
    caso: VerificarIdentidad = request.app.state.verificar_identidad
    try:
        resultado = await caso.ejecutar(sub, solicitud.selfie, solicitud.prueba_de_vida)
    except IdentidadNoDisponible as error:
        raise ErrorNegocio(
            "DEPENDENCIA_NO_DISPONIBLE",
            "La verificación de identidad no terminó",
            503,
            {"motivo": error.motivo},
        ) from error
    return VerificacionDto(
        cliente_id=resultado.cliente.id,
        estado=resultado.cliente.estado,
        resultado=resultado.resultado,
        motivo=resultado.motivo,
        verificado_en=resultado.cliente.verificado_en,
    )
