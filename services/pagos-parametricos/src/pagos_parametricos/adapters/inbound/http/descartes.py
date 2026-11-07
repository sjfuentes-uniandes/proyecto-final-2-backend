"""API interna de la cola de descarte de `parametricos-eventos` (HU-W24; H10 y H11).

La llama `bff-web` (grupo `analistas-siniestros`; decisión B1 de HU-W24, sin flecha en los
diagramas: ver `docs/hu-w24-particiones.md`). Actor por encabezados, como `polizas`: `X-Actor-Id`
(el `sub`) y `X-Actor-Rol` = `ANALISTA_SINIESTROS`; sin ellos 401, con otro rol 403.

Las acciones son idempotentes por `messageId`: repetir la misma acción sobre un mensaje ya resuelto
devuelve la resolución registrada (200), sin reenviar otra vez; otra acción, 409.
"""

import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common import correlation
from solventa_common.errors import ROL_NO_AUTORIZADO, ErrorNegocio

from pagos_parametricos.application.descartes import (
    COLA_NO_DISPONIBLE,
    ColaDescarte,
    Detalle,
    Resolucion,
)
from pagos_parametricos.domain.descarte import Accion, Causa, MensajeDescartado

ROL_ANALISTA = "ANALISTA_SINIESTROS"
MAX_SELECCION = 50
Escalar = str | int | float | bool | None


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ErrorDto(_Dto):
    codigo: str
    mensaje: str
    correlation_id: str | None
    detalles: dict[str, Any]


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


# --- Conversión -----------------------------------------------------------------------------------
def _objeto(texto: str) -> dict[str, Any] | None:
    """El sobre para mostrarlo: números como números JSON (sin Decimal, que saldría como texto)."""
    try:
        valor = json.loads(texto)
    except ValueError:
        return None
    return valor if isinstance(valor, dict) else None


def mensaje_dto(mensaje: MensajeDescartado) -> MensajeDescarte:
    return MensajeDescarte(
        message_id=mensaje.message_id,
        evento_id=mensaje.evento_id,
        tipo=mensaje.tipo,
        recibido_en=mensaje.recibido_en,
        intentos=mensaje.intentos,
        causa=mensaje.causa,
        detalle=mensaje.detalle,
        poliza=mensaje.poliza,
    )


def resolucion_dto(resolucion: Resolucion) -> ResolucionDto:
    return ResolucionDto(
        message_id=resolucion.message_id,
        evento_id=resolucion.evento_id,
        accion=resolucion.accion,
        causa=resolucion.causa,
        actor_id=resolucion.actor_id,
        motivo=resolucion.motivo,
        resuelto_en=resolucion.resuelto_en,
        corregido=_objeto(resolucion.corregido) if resolucion.corregido else None,
    )


def detalle_dto(detalle: Detalle) -> DetalleDescarte:
    return DetalleDescarte(
        mensaje=mensaje_dto(detalle.mensaje),
        contenido=_objeto(detalle.mensaje.cuerpo),
        texto=detalle.mensaje.cuerpo,
        clave_idempotencia=detalle.mensaje.evento_id,
        verificacion=dict(detalle.verificacion) if detalle.verificacion else None,
        resolucion=resolucion_dto(detalle.resolucion) if detalle.resolucion else None,
    )


# --- Dependencias y rutas -------------------------------------------------------------------------
def analista(
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_rol: Annotated[str | None, Header(alias="X-Actor-Rol")] = None,
) -> str:
    """El `sub` del analista. Se resuelve antes de validar el cuerpo: 401/403 van primero."""
    if not x_actor_id or not x_actor_rol:
        raise ErrorNegocio("ACTOR_REQUERIDO", "Faltan los encabezados del actor", 401)
    if x_actor_rol != ROL_ANALISTA:
        raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
    return x_actor_id


def _cola(request: Request) -> ColaDescarte:
    cola: ColaDescarte | None = getattr(request.app.state, "cola_descarte", None)
    if cola is None:
        raise ErrorNegocio(COLA_NO_DISPONIBLE, "La cola de descarte no está configurada", 503)
    return cola


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/parametricos/descartes",
    tags=["descartes"],
    dependencies=[Depends(analista)],
    responses={
        401: _error("ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
        403: _error("ROL_NO_AUTORIZADO: X-Actor-Rol no es ANALISTA_SINIESTROS"),
        503: _error("COLA_NO_DISPONIBLE"),
    },
)

Analista = Annotated[str, Depends(analista)]
Cola = Annotated[ColaDescarte, Depends(_cola)]
IdMensaje = Annotated[UUID, Path(alias="messageId")]
_NO_ENCONTRADO = _error("DESCARTE_NO_ENCONTRADO: no está en la DLQ o lo revisa otro analista")
_YA_RESUELTO = _error("DESCARTE_YA_RESUELTO: el mensaje ya tiene otra resolución")


@router.get("", response_model=ListadoDescarte)
async def listar(cola: Cola) -> ListadoDescarte:
    """H10: los mensajes de la DLQ, sin consumirlos, con su causa y el resumen por causa."""
    listado = await cola.listar()
    return ListadoDescarte(
        mensajes=[mensaje_dto(m) for m in listado.mensajes],
        por_causa=[
            ConteoCausa(causa=causa, cantidad=cantidad)
            for causa, cantidad in sorted(listado.por_causa.items(), key=lambda par: -par[1])
        ],
        total=len(listado.mensajes),
        retencion_dias=listado.retencion_dias,
        max_intentos=listado.max_intentos,
    )


@router.get("/{messageId}", response_model=DetalleDescarte, responses={404: _NO_ENCONTRADO})
async def detalle(message_id: IdMensaje, cola: Cola) -> DetalleDescarte:
    """H11: el contenido recibido, la causa, la clave de idempotencia y la verificación."""
    return detalle_dto(await cola.detalle(str(message_id)))


@router.post("/reproceso", response_model=RespuestaReproceso, responses={409: _YA_RESUELTO})
async def reprocesar(
    solicitud: SolicitudReproceso, cola: Cola, actor: Analista
) -> RespuestaReproceso:
    """H10 "Reprocesar seleccionados": reenvía cada uno a la cola origen con el mismo eventId."""
    ids = list(dict.fromkeys(str(m) for m in solicitud.message_ids))
    resultados = await cola.reprocesar(ids, actor, correlation.obtener() or "")
    return RespuestaReproceso(
        resultados=[
            ResultadoReproceso(
                message_id=mid,
                resolucion=resolucion_dto(r) if r and r.accion is Accion.REPROCESADO else None,
            )
            for mid, r in resultados.items()
        ]
    )


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
    message_id: IdMensaje, solicitud: SolicitudCorreccion, cola: Cola, actor: Analista
) -> ResolucionDto:
    """H11 "Corregir y reprocesar": guarda original y corrección en auditoría y reenvía."""
    resolucion = await cola.corregir(
        str(message_id),
        solicitud.cambios,
        actor,
        correlation.obtener() or "",
        solicitud.motivo,
    )
    return resolucion_dto(resolucion)


@router.post(
    "/{messageId}/descarte",
    response_model=ResolucionDto,
    responses={404: _NO_ENCONTRADO, 409: _YA_RESUELTO},
)
async def descartar(
    message_id: IdMensaje, solicitud: SolicitudDescarte, cola: Cola, actor: Analista
) -> ResolucionDto:
    """H11 "Descartar definitivamente": lo borra de la DLQ y deja la resolución auditada."""
    resolucion = await cola.descartar(
        str(message_id), actor, correlation.obtener() or "", solicitud.motivo
    )
    return resolucion_dto(resolucion)
