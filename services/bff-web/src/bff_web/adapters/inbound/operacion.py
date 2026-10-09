"""GET /operacion/trazas: traza de un recorrido para el back-office (grupo operacion).

API Gateway publica la ruta como /web/operacion/trazas, valida el token con el
autorizador de Cognito y sobrescribe X-Authenticated-Issuer y
X-Authenticated-Groups con los claims del token (infra/platform/edge.tf).
"""

import asyncio
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from solventa_common.correlation import es_valido

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.outbound.xray import crear_repositorio
from bff_web.application.consultar_traza import ConsultarTraza
from bff_web.config import settings
from bff_web.domain.trazas import DetalleTraza, FiltroTrazas

router = APIRouter(prefix="/operacion", tags=["operacion"])


class TramoRespuesta(BaseModel):
    servicio: str
    operacion: str
    externo: bool
    inicio_ms: int
    duracion_ms: int
    propio_ms: int
    participacion: float
    estado: str
    destacado: bool


class ContextoRespuesta(BaseModel):
    canal: str
    servicio_entrada: str
    servicios_internos: int
    servicios_externos: int
    ambiente: str


class TrazaRespuesta(BaseModel):
    correlation_id: str | None
    trace_ids: list[str]
    inicio: datetime
    duracion_total_ms: int
    umbral_ms: int
    resultado: str
    tramos: list[TramoRespuesta]
    interpretacion: str
    contexto: ContextoRespuesta


@lru_cache
def consultar_traza() -> ConsultarTraza:
    return ConsultarTraza(crear_repositorio(settings.aws_region), settings.umbral_recorrido_ms)


def _respuesta(detalle: DetalleTraza, umbral_ms: int) -> TrazaRespuesta:
    return TrazaRespuesta(
        correlation_id=detalle.correlation_id,
        trace_ids=detalle.trace_ids,
        inicio=detalle.inicio,
        duracion_total_ms=detalle.duracion_total_ms,
        umbral_ms=umbral_ms,
        resultado=detalle.resultado,
        tramos=[
            TramoRespuesta(
                servicio=t.servicio, operacion=t.operacion, externo=t.externo, inicio_ms=t.inicio_ms,
                duracion_ms=t.duracion_ms, propio_ms=t.propio_ms, participacion=t.participacion,
                estado=t.estado, destacado=t.destacado,
            )
            for t in detalle.tramos
        ],
        interpretacion=detalle.interpretacion,
        contexto=ContextoRespuesta(
            canal=detalle.contexto.get("canal", detalle.servicio_entrada),
            servicio_entrada=detalle.servicio_entrada,
            servicios_internos=detalle.servicios_internos,
            servicios_externos=detalle.servicios_externos,
            ambiente=settings.environment,
        ),
    )


@router.get("/trazas", response_model=TrazaRespuesta, dependencies=[Depends(requerir_grupo(settings.grupo_operacion))])
async def obtener_traza(
    caso: Annotated[ConsultarTraza, Depends(consultar_traza)],
    correlation_id: Annotated[str | None, Query(max_length=128)] = None,
    recorrido: Annotated[Literal["api-socios", "bff-web", "bff-movil"] | None, Query()] = None,
    duracion_min_ms: Annotated[int, Query(ge=0, le=600_000)] = 0,
    horas: Annotated[int, Query(ge=1, le=24)] = 6,
) -> TrazaRespuesta:
    if correlation_id is not None and not es_valido(correlation_id):
        raise HTTPException(400, "Identificador inválido: hasta 128 caracteres de [A-Za-z0-9._:-]")
    filtro = FiltroTrazas(correlation_id=correlation_id, entrada=recorrido, duracion_min_ms=duracion_min_ms, horas=horas)
    # boto3 es bloqueante: se ejecuta fuera del event loop.
    detalle = await asyncio.to_thread(caso.ejecutar, filtro)
    if detalle is None:
        raise HTTPException(404, "No hay trazas para ese filtro en el periodo consultado")
    return _respuesta(detalle, caso.umbral_ms)
