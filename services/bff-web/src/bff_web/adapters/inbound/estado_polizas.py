"""HU-W18: estado vigente y listado de pólizas en el portal (grupo asesores).

- `GET /v1/polizas`: listado paginado con filtros `numero`, `ramo`, `estado` y `vigenciaDesde`.
- `GET /v1/polizas/{polizaId}/estado`: el estado vigente y su fecha de actualización.

El estado (NO_INICIADA, VIGENTE, POR_VENCER o VENCIDA) lo deriva consulta y se reenvía tal cual. Todo
asesor ve todas las pólizas proyectadas; la que no existe o aún no se proyectó es un único 404. Solo
los parámetros de los contratos llegan a consulta: nada del navegador (cliente, socio, rol) altera
el resultado. El cuerpo de consulta se valida contra el DTO antes de devolverlo: si no lo cumple,
503 (falla cerrada). El detalle agrega el titular (nombre, tipo y número enmascarado) que pide a
clientes con el `clienteId` de consulta, que no sale al navegador; si clientes falla o tarda, los
tres campos van en null y el estado sale igual. Los DTO son copia de los de consulta
(contracts/openapi/consulta.yaml), salvo el titular.
"""

from datetime import date, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from pydantic import Field, TypeAdapter, ValidationError
from solventa_common.errors import RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.cotizaciones import CoberturaProductoDto, TextoDecimal
from bff_web.adapters.inbound.perfilamiento import actor
from bff_web.adapters.inbound.polizas import (
    PatronNumero,
    VigenciaPolizaDto,
    _Dto,
)
from bff_web.adapters.inbound.socios import ErrorDto
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.consulta import ConsultaCliente
from bff_web.application.estado_poliza import EstadoPolizas, servicio_no_disponible
from bff_web.config import settings

TAMANO_MAXIMO = 50


class EstadoVigencia(StrEnum):
    NO_INICIADA = "NO_INICIADA"
    VIGENTE = "VIGENTE"
    POR_VENCER = "POR_VENCER"
    VENCIDA = "VENCIDA"


class EstadoPolizaDto(_Dto):
    """Estado vigente de una póliza; `fechaActualizacion` es cuando la proyección la registró."""

    poliza_id: UUID
    numero: PatronNumero
    estado: EstadoVigencia
    fecha_actualizacion: datetime
    producto: str = Field(description="Nombre del producto")
    ramo: str = Field(description="Nombre del ramo")
    vigencia: VigenciaPolizaDto
    prima: TextoDecimal
    suma_asegurada: TextoDecimal
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    periodicidad: str
    emitida_en: datetime
    coberturas: list[CoberturaProductoDto]
    titular_nombre: str | None = Field(
        description="Nombre del titular según clientes; nulo si clientes no lo da a tiempo"
    )
    tipo_documento: Annotated[str, Field(pattern=r"^[A-Z]{2,3}$")] | None = Field(
        description="Tipo del documento del titular; nulo si clientes no lo da a tiempo"
    )
    documento_enmascarado: str | None = Field(
        description="Número de documento enmascarado por clientes (p. ej. 1.020.***.456); "
        "el número completo nunca llega al navegador. Nulo si clientes no lo da a tiempo"
    )


class ResumenPolizaDto(_Dto):
    poliza_id: UUID
    numero: PatronNumero
    ramo: str
    producto: str
    prima: TextoDecimal
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    vigencia_hasta: date
    estado: EstadoVigencia
    fecha_actualizacion: datetime


class PaginaPolizasDto(_Dto):
    items: list[ResumenPolizaDto]
    total: int
    pagina: int
    tamano: int
    actualizado_en: datetime | None = Field(
        description="La actualización más reciente de la proyección; nulo si está vacía"
    )


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    tags=["estado-polizas"],
    route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_asesores))],
    responses={
        401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
        403: _error("ROL_NO_AUTORIZADO: falta el grupo asesores o no es del back-office"),
        422: _error("SOLICITUD_INVALIDA"),
        503: _error("SERVICIO_NO_DISPONIBLE"),
    },
)


@lru_cache
def estados() -> EstadoPolizas:
    return EstadoPolizas(
        ConsultaCliente(settings.consulta_url, settings.consulta_timeout_ms),
        ClientesCliente(settings.clientes_url, settings.clientes_timeout_ms),
        settings.nombre_cliente_timeout_ms,
    )


Servicio = Annotated[EstadoPolizas, Depends(estados)]
Actor = Annotated[str, Depends(actor)]
IdPoliza = Annotated[UUID, Path(alias="polizaId")]
_ESTADO = TypeAdapter(EstadoPolizaDto)
_PAGINA = TypeAdapter(PaginaPolizasDto)


def _validar(adaptador: TypeAdapter[Any], cuerpo: Any) -> Any:
    """El cuerpo de consulta, si cumple el DTO; si no, 503."""
    try:
        adaptador.validate_python(cuerpo)
    except ValidationError as error:
        raise servicio_no_disponible() from error
    return cuerpo


@router.get(
    "/v1/polizas",
    response_model=PaginaPolizasDto,
    summary="Listado paginado de pólizas con su estado, de la más reciente a la más antigua",
)
async def listar_polizas(
    servicio: Servicio,
    asesor: Actor,
    numero: Annotated[
        str | None,
        Query(pattern=r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$", description="Coincidencia exacta"),
    ] = None,
    ramo: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
    estado: EstadoVigencia | None = None,
    vigencia_desde: Annotated[
        date | None,
        Query(alias="vigenciaDesde", description="Pólizas cuya vigencia termina ese día o después"),
    ] = None,
    pagina: Annotated[int, Query(ge=1, le=100_000)] = 1,
    tamano: Annotated[int, Query(ge=1, le=TAMANO_MAXIMO)] = 20,
) -> Any:
    parametros: dict[str, Any] = {"pagina": pagina, "tamano": tamano}
    if numero is not None:
        parametros["numero"] = numero
    if ramo is not None:
        parametros["ramo"] = ramo
    if estado is not None:
        parametros["estado"] = estado.value
    if vigencia_desde is not None:
        parametros["vigenciaDesde"] = vigencia_desde.isoformat()
    return _validar(_PAGINA, await servicio.listar(parametros, asesor))


@router.get(
    "/v1/polizas/{polizaId}/estado",
    response_model=EstadoPolizaDto,
    summary="Estado vigente de una póliza y su fecha de actualización",
    responses={
        404: _error("POLIZA_NO_ENCONTRADA: no existe o aún no se proyectó (no se distingue)")
    },
)
async def estado_de_poliza(poliza_id: IdPoliza, servicio: Servicio, asesor: Actor) -> Any:
    return _validar(_ESTADO, await servicio.estado(poliza_id, asesor))
