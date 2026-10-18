"""HU-W30: alta del cliente y verificación de su identidad desde el portal. Solo el grupo asesores.

Los DTO son copia de los de clientes (contracts/openapi/clientes.yaml) con los mismos nombres:
tests/contract/test_openapi.py verifica que sus esquemas sean iguales. La respuesta lleva el
estado de la identidad y la traza de la verificación, nunca PII ni el motivo del proveedor.
"""

from datetime import date, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.perfilamiento import Actor
from bff_web.adapters.inbound.socios import ErrorDto
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.application.clientes import Clientes
from bff_web.config import settings


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/clientes",
    tags=["clientes"],
    route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_asesores))],
    responses={
        401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
        403: _error("ROL_NO_AUTORIZADO: falta el grupo asesores o no es del back-office"),
        422: _error("SOLICITUD_INVALIDA (detalles.campos)"),
    },
)


class EstadoIdentidad(StrEnum):
    VERIFICADO = "VERIFICADO"
    PENDIENTE = "PENDIENTE"
    RECHAZADO = "RECHAZADO"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SolicitudAltaCliente(_Dto):
    tipo_documento: Annotated[str, Field(pattern=r"^[A-Z]{2,3}$")]
    numero_documento: Annotated[str, Field(pattern=r"^[A-Za-z0-9]{3,20}$")]
    nombre_completo: Annotated[str, Field(min_length=1, max_length=200)]
    fecha_nacimiento: date


class ClienteDto(_Dto):
    """Estado del cliente y traza de la verificación. No incluye PII ni el motivo del proveedor."""

    cliente_id: UUID
    estado: EstadoIdentidad
    verificado_en: datetime | None
    proveedor: str | None
    referencia: str | None


@lru_cache
def clientes() -> Clientes:
    return Clientes(ClientesCliente(settings.clientes_url, settings.clientes_timeout_ms))


Servicio = Annotated[Clientes, Depends(clientes)]
IdCliente = Annotated[UUID, Path(alias="clienteId")]


@router.post(
    "",
    response_model=ClienteDto,
    response_model_by_alias=True,
    status_code=201,
    summary="Da de alta al cliente en estado PENDIENTE",
    responses={
        200: {"model": ClienteDto, "description": "El documento ya tenía cliente: se reutiliza"},
        503: _error("SERVICIO_NO_DISPONIBLE"),
    },
)
async def crear_cliente(
    solicitud: SolicitudAltaCliente, http: Response, servicio: Servicio, asesor: Actor
) -> dict[str, Any]:
    """El mismo documento devuelve el mismo cliente, sin duplicarlo (paso 1 de la cotización)."""
    estado, cliente = await servicio.crear(solicitud.model_dump(mode="json", by_alias=True), asesor)
    http.status_code = estado
    return cliente


@router.get(
    "/{clienteId}",
    response_model=ClienteDto,
    response_model_by_alias=True,
    summary="Estado de la identidad y traza de la verificación del cliente",
    responses={404: _error("CLIENTE_NO_ENCONTRADO"), 503: _error("SERVICIO_NO_DISPONIBLE")},
)
async def consultar_cliente(
    cliente_id: IdCliente, servicio: Servicio, asesor: Actor
) -> dict[str, Any]:
    return await servicio.obtener(cliente_id, asesor)


@router.post(
    "/{clienteId}/identidad/verificaciones",
    response_model=ClienteDto,
    response_model_by_alias=True,
    summary="Pide al proveedor que verifique la identidad del cliente",
    responses={
        404: _error("CLIENTE_NO_ENCONTRADO"),
        409: _error("CLIENTE_SIN_DOCUMENTO"),
        503: _error(
            "DEPENDENCIA_NO_DISPONIBLE: el proveedor no respondió; no es un rechazo y se puede "
            "reintentar (detalles.estado); o SERVICIO_NO_DISPONIBLE"
        ),
    },
)
async def verificar_cliente(
    cliente_id: IdCliente, servicio: Servicio, asesor: Actor
) -> dict[str, Any]:
    """Un cliente ya VERIFICADO responde su estado sin consultar de nuevo al proveedor."""
    return await servicio.verificar(cliente_id, asesor)
