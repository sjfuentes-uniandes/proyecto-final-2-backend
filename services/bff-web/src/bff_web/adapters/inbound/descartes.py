"""HU-W24: cola de descarte de los eventos paramétricos en el portal (grupo analistas-siniestros;
pantallas H10 y H11 en /siniestros/parametricos/descarte).

- `GET /v1/siniestros/parametricos/descartes`: los mensajes de la DLQ, sin consumirlos, con su
  causa, intentos y póliza referida, y el resumen por causa (H10).
- `GET /v1/siniestros/parametricos/descartes/{messageId}`: el contenido recibido, la clave de
  idempotencia y la verificación de HU-W21 si la hay (H11).
- `POST …/reproceso` (seleccionados), `POST …/{messageId}/correccion` y `POST …/{messageId}/descarte`.

El actor es el `sub` (X-Authenticated-Sub); pagos-parametricos lo registra en la auditoría. Los DTO
son copia de los de pagos-parametricos (contracts/openapi/pagos-parametricos.yaml): el cuerpo se
valida contra ellos antes de devolverlo; si no lo cumple, 503 (falla cerrada).
"""

from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.errors import RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.perfilamiento import actor
from bff_web.adapters.inbound.socios import ErrorDto
from bff_web.adapters.outbound.pagos_parametricos import PagosParametricosCliente
from bff_web.application.descartes import Descartes, servicio_no_disponible
from bff_web.config import settings

MAX_SELECCION = 50
Escalar = str | int | float | bool | None


# --- Enumeraciones y DTO (copia de pagos-parametricos) ---------------------------------------
class Causa(StrEnum):
    FORMATO_INVALIDO = "FORMATO_INVALIDO"
    REFERENCIA_INEXISTENTE = "REFERENCIA_INEXISTENTE"
    ERROR_PROCESAMIENTO = "ERROR_PROCESAMIENTO"
    DESCONOCIDA = "DESCONOCIDA"


class Accion(StrEnum):
    REPROCESADO = "REPROCESADO"
    CORREGIDO = "CORREGIDO"
    DESCARTADO = "DESCARTADO"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class MensajeDescarte(_Dto):
    message_id: str
    evento_id: str | None = Field(description="El eventId: también es la clave de idempotencia")
    tipo: str | None
    recibido_en: str | None = Field(description="Cuándo llegó el evento a la cola (SentTimestamp)")
    intentos: int | None
    causa: Causa
    detalle: str | None = Field(description="Tipo de error o motivo que anotó el consumidor")
    poliza: str | None = Field(description="La póliza a la que se refiere el evento, si la trae")


class ConteoCausa(_Dto):
    causa: Causa
    cantidad: int


class ListadoDescarte(_Dto):
    mensajes: list[MensajeDescarte]
    por_causa: list[ConteoCausa]
    total: int
    retencion_dias: int = Field(description="Conservación de la DLQ (14 días en catalog.tf)")
    max_intentos: int = Field(description="maxReceiveCount de la cola origen")


class ResolucionDto(_Dto):
    message_id: str
    evento_id: str | None
    accion: Accion
    causa: Causa
    actor_id: str
    motivo: str | None
    resuelto_en: datetime
    corregido: dict[str, Any] | None = Field(description="El sobre reenviado, solo en CORREGIDO")


class DetalleDescarte(_Dto):
    mensaje: MensajeDescarte
    contenido: dict[str, Any] | None = Field(description="El sobre recibido; null si no es JSON")
    texto: str = Field(description="El cuerpo tal como está en la DLQ")
    clave_idempotencia: str | None
    verificacion: dict[str, Any] | None = Field(
        description="Póliza vigente, cobertura activa y pago previo (HU-W21); null si no hay"
    )
    resolucion: ResolucionDto | None


class SolicitudReproceso(_Dto):
    message_ids: list[UUID] = Field(min_length=1, max_length=MAX_SELECCION)


class ResultadoReproceso(_Dto):
    message_id: str
    resolucion: ResolucionDto | None = Field(description="null si no está en la cola de descarte")


class RespuestaReproceso(_Dto):
    resultados: list[ResultadoReproceso]


class SolicitudCorreccion(_Dto):
    cambios: dict[Annotated[str, Field(min_length=1, max_length=64)], Escalar] = Field(
        min_length=1, max_length=20, description="Campos del payload y su valor corregido"
    )
    motivo: str | None = Field(default=None, max_length=500)


class SolicitudDescarte(_Dto):
    motivo: str = Field(min_length=1, max_length=500)


# --- Dependencias y rutas -------------------------------------------------------------------
def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/siniestros/parametricos/descartes",
    tags=["descartes"],
    route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_analistas_siniestros))],
    responses={
        401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
        403: _error(
            "ROL_NO_AUTORIZADO: falta el grupo analistas-siniestros o no es del back-office"
        ),
        503: _error("SERVICIO_NO_DISPONIBLE"),
    },
)


@lru_cache
def descartes() -> Descartes:
    return Descartes(
        PagosParametricosCliente(
            settings.pagos_parametricos_url, settings.pagos_parametricos_timeout_ms
        )
    )


Servicio = Annotated[Descartes, Depends(descartes)]
Actor = Annotated[str, Depends(actor)]
IdMensaje = Annotated[UUID, Path(alias="messageId")]
_NO_ENCONTRADO = _error("DESCARTE_NO_ENCONTRADO: no está en la DLQ o lo revisa otro analista")
_YA_RESUELTO = _error("DESCARTE_YA_RESUELTO: el mensaje ya tiene otra resolución")
_LISTADO = TypeAdapter(ListadoDescarte)
_DETALLE = TypeAdapter(DetalleDescarte)
_REPROCESO = TypeAdapter(RespuestaReproceso)
_RESOLUCION = TypeAdapter(ResolucionDto)


def _validar(adaptador: TypeAdapter[Any], cuerpo: Any) -> Any:
    try:
        adaptador.validate_python(cuerpo)
    except ValidationError as error:
        raise servicio_no_disponible() from error
    return cuerpo


@router.get("", response_model=ListadoDescarte)
async def listar(servicio: Servicio, analista: Actor) -> Any:
    """H10: los eventos que agotaron sus reintentos, con causa, intentos y póliza referida."""
    return _validar(_LISTADO, await servicio.listar(analista))


@router.get(
    "/{messageId}",
    response_model=DetalleDescarte,
    responses={404: _NO_ENCONTRADO, 422: _error("SOLICITUD_INVALIDA")},
)
async def detalle(message_id: IdMensaje, servicio: Servicio, analista: Actor) -> Any:
    """H11: el contenido recibido, la causa y la verificación antes de reprocesar."""
    return _validar(_DETALLE, await servicio.detalle(str(message_id), analista))


@router.post(
    "/reproceso",
    response_model=RespuestaReproceso,
    responses={409: _YA_RESUELTO, 422: _error("SOLICITUD_INVALIDA")},
)
async def reprocesar(solicitud: SolicitudReproceso, servicio: Servicio, analista: Actor) -> Any:
    """H10 "Reprocesar seleccionados": cada uno vuelve a la cola con el mismo eventId."""
    ids = [str(m) for m in solicitud.message_ids]
    return _validar(_REPROCESO, await servicio.reprocesar(ids, analista))


@router.post(
    "/{messageId}/correccion",
    response_model=ResolucionDto,
    responses={
        404: _NO_ENCONTRADO,
        409: _YA_RESUELTO,
        422: _error("CORRECCION_INVALIDA (detalles.campos) o SOLICITUD_INVALIDA"),
    },
)
async def corregir(
    message_id: IdMensaje, solicitud: SolicitudCorreccion, servicio: Servicio, analista: Actor
) -> Any:
    """H11 "Corregir y reprocesar": el original y la corrección quedan en la auditoría."""
    cuerpo = solicitud.model_dump(mode="json", by_alias=True)
    return _validar(_RESOLUCION, await servicio.corregir(str(message_id), cuerpo, analista))


@router.post(
    "/{messageId}/descarte",
    response_model=ResolucionDto,
    responses={404: _NO_ENCONTRADO, 409: _YA_RESUELTO, 422: _error("SOLICITUD_INVALIDA")},
)
async def descartar(
    message_id: IdMensaje, solicitud: SolicitudDescarte, servicio: Servicio, analista: Actor
) -> Any:
    """H11 "Descartar definitivamente": sale de la DLQ con registro auditado."""
    cuerpo = solicitud.model_dump(mode="json", by_alias=True)
    return _validar(_RESOLUCION, await servicio.descartar(str(message_id), cuerpo, analista))
