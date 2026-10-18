"""HU-W31: consentimientos del cliente para el asesor, de solo lectura (ADR 0002). Solo el grupo
asesores; no hay rutas para registrar ni revocar.

Los DTO son copia de los de clientes (contracts/openapi/clientes.yaml): tests/contract/
test_openapi.py verifica que sus esquemas sean iguales. `ConsentimientoDto` ya es en este BFF el
de cotizacion (dentro del perfil), así que el de clientes se publica como
`ConsentimientoClienteDto`, con las mismas propiedades.

Transmiten lo que responde clientes (`response_model_exclude_unset`): las claves del DTO pasan sin
cambios, también las que valen null. Un cuerpo de clientes que no valida contra el DTO (por
ejemplo, un estado que este BFF no conoce) es 503: falla cerrado.
"""

from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.errors import RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.perfilamiento import Actor
from bff_web.adapters.inbound.socios import ErrorDto
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.application.consentimientos import Consentimientos, servicio_no_disponible
from bff_web.config import settings


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/clientes",
    tags=["consentimientos"],
    route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_asesores))],
    responses={
        401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
        403: _error("ROL_NO_AUTORIZADO: falta el grupo asesores o no es del back-office"),
        422: _error("SOLICITUD_INVALIDA (detalles.campos)"),
        503: _error(
            "SERVICIO_NO_DISPONIBLE: clientes no respondió o respondió fuera de su contrato "
            "(falla cerrado, sin detalles)"
        ),
    },
)


class EstadoConsentimiento(StrEnum):
    ACTIVO = "ACTIVO"
    REVOCADO = "REVOCADO"
    VENCIDO = "VENCIDO"


class TipoHito(StrEnum):
    OTORGADO = "OTORGADO"
    REVOCADO = "REVOCADO"
    VENCIDO = "VENCIDO"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ConsentimientoClienteDto(_Dto):
    consentimiento_id: UUID
    estado: EstadoConsentimiento
    proposito: str
    alcances: list[str]
    version_texto: str
    idioma: str
    otorgado_en: datetime
    vigente_hasta: datetime
    revocado_en: datetime | None


class HitoDto(_Dto):
    tipo: TipoHito
    ocurrido_en: datetime


class HistorialDto(_Dto):
    """Cuándo se otorgó y, si aplica, cuándo se revocó o venció (AC4). Derivado de la fila."""

    consentimiento_id: UUID
    estado: EstadoConsentimiento
    hitos: list[HitoDto]


_LISTA = TypeAdapter(list[ConsentimientoClienteDto])
_HISTORIAL = TypeAdapter(HistorialDto)


def _en_contrato(adaptador: TypeAdapter[Any], cuerpo: Any) -> Any:
    """Devuelve el cuerpo de clientes tal cual si cumple el DTO; si no, 503."""
    try:
        adaptador.validate_python(cuerpo)
    except ValidationError as error:
        raise servicio_no_disponible() from error
    return cuerpo


@lru_cache
def consentimientos() -> Consentimientos:
    return Consentimientos(ClientesCliente(settings.clientes_url, settings.clientes_timeout_ms))


Servicio = Annotated[Consentimientos, Depends(consentimientos)]
IdCliente = Annotated[UUID, Path(alias="clienteId")]
IdConsentimiento = Annotated[UUID, Path(alias="consentimientoId")]


@router.get(
    "/{clienteId}/consentimientos",
    response_model=list[ConsentimientoClienteDto],
    response_model_exclude_unset=True,
    summary="Consentimientos del cliente, del más reciente al más antiguo (solo lectura)",
    responses={404: _error("CLIENTE_NO_ENCONTRADO: el cliente no existe")},
)
async def listar_consentimientos(cliente_id: IdCliente, servicio: Servicio, asesor: Actor) -> Any:
    """El estado es el que deriva clientes al leer (`VENCIDO` si ya pasó `vigenteHasta`). Un
    cliente sin consentimientos responde una lista vacía."""
    return _en_contrato(_LISTA, await servicio.listar(cliente_id, asesor))


@router.get(
    "/{clienteId}/consentimientos/{consentimientoId}/historial",
    response_model=HistorialDto,
    response_model_exclude_unset=True,
    summary="Historial de un consentimiento del cliente (solo lectura)",
    responses={
        404: _error(
            "CONSENTIMIENTO_NO_ENCONTRADO: no existe, es de otro cliente o el cliente no existe "
            "(no se distingue)"
        )
    },
)
async def historial_consentimiento(
    cliente_id: IdCliente,
    consentimiento_id: IdConsentimiento,
    servicio: Servicio,
    asesor: Actor,
) -> Any:
    return _en_contrato(_HISTORIAL, await servicio.historial(cliente_id, consentimiento_id, asesor))
