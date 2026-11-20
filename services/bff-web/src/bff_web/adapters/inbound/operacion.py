"""GET /operacion/trazas, /operacion/alertas y /operacion/capacidad para el back-office (grupo
operacion).

API Gateway publica la ruta como /web/operacion/trazas, valida el token con el
autorizador de Cognito y sobrescribe X-Authenticated-Issuer y
X-Authenticated-Groups con los claims del token (infra/platform/edge.tf).
"""

import asyncio
import logging
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from solventa_common.correlation import es_valido
from solventa_common.errors import ErrorNegocio, RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.socios import ErrorDto, api_socios
from bff_web.adapters.outbound import capacidad, cloudwatch
from bff_web.adapters.outbound.api_socios import ApiSocios
from bff_web.adapters.outbound.xray import crear_repositorio
from bff_web.application.consultar_alertas import ConsultarAlertas
from bff_web.application.consultar_capacidad import ConsultarCapacidad
from bff_web.application.consultar_traza import ConsultarTraza
from bff_web.config import settings
from bff_web.domain.alertas import AlertaAbierta, PanelAlertas, leer_catalogo, leer_umbrales
from bff_web.domain.capacidad import PanelCapacidad
from bff_web.domain.trazas import DetalleTraza, FiltroTrazas

router = APIRouter(prefix="/operacion", tags=["operacion"])
logger = logging.getLogger("bff_web.operacion")


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
                servicio=t.servicio,
                operacion=t.operacion,
                externo=t.externo,
                inicio_ms=t.inicio_ms,
                duracion_ms=t.duracion_ms,
                propio_ms=t.propio_ms,
                participacion=t.participacion,
                estado=t.estado,
                destacado=t.destacado,
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


@router.get(
    "/trazas",
    response_model=TrazaRespuesta,
    dependencies=[Depends(requerir_grupo(settings.grupo_operacion))],
)
async def obtener_traza(
    caso: Annotated[ConsultarTraza, Depends(consultar_traza)],
    correlation_id: Annotated[str | None, Query(max_length=128)] = None,
    recorrido: Annotated[Literal["api-socios", "bff-web", "bff-movil"] | None, Query()] = None,
    duracion_min_ms: Annotated[int, Query(ge=0, le=600_000)] = 0,
    horas: Annotated[int, Query(ge=1, le=24)] = 6,
) -> TrazaRespuesta:
    if correlation_id is not None and not es_valido(correlation_id):
        raise HTTPException(400, "Identificador inválido: hasta 128 caracteres de [A-Za-z0-9._:-]")
    filtro = FiltroTrazas(
        correlation_id=correlation_id,
        entrada=recorrido,
        duracion_min_ms=duracion_min_ms,
        horas=horas,
    )
    # boto3 es bloqueante: se ejecuta fuera del event loop.
    detalle = await asyncio.to_thread(caso.ejecutar, filtro)
    if detalle is None:
        raise HTTPException(404, "No hay trazas para ese filtro en el periodo consultado")
    return _respuesta(detalle, caso.umbral_ms)


# --- HU-W28: Operación › Alertas ----------------------------------------------------------
class EnlaceRespuesta(BaseModel):
    tipo: Literal["traza", "socio", "cola", "tablero"]
    url: str | None = None
    socio_id: str | None = None
    min_ms: int | None = None


class AlertaRespuesta(BaseModel):
    alarma: str
    severidad: Literal["critica", "advertencia", "informativa"]
    titulo: str
    valor_actual: float | None
    umbral: float
    unidad: str
    abierta_desde: datetime
    enlace: EnlaceRespuesta


class UmbralRespuesta(BaseModel):
    metrica: str
    unidad: str
    meta: float
    advertencia: float
    critica: float


class ResumenRespuesta(BaseModel):
    desde: datetime
    generadas: int
    criticas: int
    falsos_positivos: int
    tiempo_medio_atencion_min: int | None
    metas_incumplidas: int


class AlertasRespuesta(BaseModel):
    ambiente: str
    porcentaje_advertencia: float
    falso_positivo_min: float
    atencion_min: float
    abiertas: list[AlertaRespuesta]
    umbrales: list[UmbralRespuesta]
    resumen: ResumenRespuesta


@lru_cache
def consultar_alertas() -> ConsultarAlertas:
    return ConsultarAlertas(
        cloudwatch.crear_repositorio(settings.aws_region, settings.environment),
        settings.alertas_prefijo,
        leer_catalogo(settings.alertas_catalogo),
        leer_umbrales(settings.alertas_umbrales),
        settings.alertas_falso_positivo_min,
        settings.alertas_atencion_min,
    )


async def _nombre_de_socio(alerta: AlertaAbierta, socios: ApiSocios) -> str:
    socio_id = alerta.enlace.get("socio_id")
    if not socio_id:
        return alerta.titulo
    try:
        nombre = (await socios.obtener(socio_id, None)).get("nombre") or socio_id
    except Exception as error:  # noqa: BLE001 - sin nombre se muestra el identificador
        logger.warning(
            "no se pudo leer el socio de la alerta",
            extra={"socio_id": socio_id, "error_type": type(error).__name__},
        )
        nombre = socio_id
    return alerta.titulo.replace("Socio ", f"Socio {nombre} ", 1)


async def _respuesta_alertas(panel: PanelAlertas, socios: ApiSocios) -> AlertasRespuesta:
    return AlertasRespuesta(
        ambiente=settings.environment,
        porcentaje_advertencia=settings.alertas_advertencia_pct,
        falso_positivo_min=settings.alertas_falso_positivo_min,
        atencion_min=settings.alertas_atencion_min,
        abiertas=[
            AlertaRespuesta(
                alarma=a.alarma,
                severidad=a.severidad,
                titulo=await _nombre_de_socio(a, socios),
                valor_actual=a.valor_actual,
                umbral=a.umbral,
                unidad=a.unidad,
                abierta_desde=a.abierta_desde,
                enlace=EnlaceRespuesta(**a.enlace),
            )
            for a in panel.abiertas
        ],
        umbrales=[UmbralRespuesta(**vars(u)) for u in panel.umbrales],
        resumen=ResumenRespuesta(**vars(panel.resumen)),
    )


@router.get(
    "/alertas",
    response_model=AlertasRespuesta,
    dependencies=[Depends(requerir_grupo(settings.grupo_operacion))],
)
async def obtener_alertas(
    caso: Annotated[ConsultarAlertas, Depends(consultar_alertas)],
    socios: Annotated[ApiSocios, Depends(api_socios)],
) -> AlertasRespuesta:
    try:
        panel = await asyncio.to_thread(caso.ejecutar)
    except Exception as error:  # CloudWatch no disponible o sin permisos
        logger.error("no se pudieron leer las alarmas", extra={"error_type": type(error).__name__})
        raise HTTPException(502, "No se pudieron leer las alarmas del ambiente") from error
    return await _respuesta_alertas(panel, socios)


# --- HU-W08: Operación › Tablero, cotizaciones por minuto y capacidad -----------------------
# Errores en formato B2 ({codigo, mensaje, correlationId, detalles}) solo en esta ruta: /trazas
# y /alertas conservan {"detail"}. Si falla AWS o falta la configuración, 503
# SERVICIO_NO_DISPONIBLE sin detalles (contracts/codigos-error.md).
def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router_capacidad = APIRouter(
    route_class=RutaB2,
    responses={
        403: _error("ROL_NO_AUTORIZADO: falta el grupo operacion o no es del back-office"),
        503: _error(
            "SERVICIO_NO_DISPONIBLE: CloudWatch o Application Auto Scaling no respondieron, "
            "o el ambiente no tiene configurada la capacidad (sin detalles)"
        ),
    },
)


class PuntoCapacidadRespuesta(BaseModel):
    instante: datetime
    valor: int


class EscaladoRespuesta(BaseModel):
    inicio: datetime
    fin: datetime | None
    duracion_s: int | None
    descripcion: str
    estado: str


class CapacidadRespuesta(BaseModel):
    ambiente: str
    servicio: str
    ventana_min: int
    cotizaciones_por_minuto: list[PuntoCapacidadRespuesta]
    tareas_activas: int | None  # null sin puntos de CPUUtilization en la ventana
    minimo: int
    maximo: int
    ultimo_escalado: EscaladoRespuesta | None


class CapacidadNoConfigurada(Exception):
    """El ambiente no entrega CAPACIDAD_CLUSTER y CAPACIDAD_SERVICIO (infra/apps/ecs.tf)."""


@lru_cache
def consultar_capacidad() -> ConsultarCapacidad | None:
    if not (settings.capacidad_cluster and settings.capacidad_servicio):
        return None
    return ConsultarCapacidad(
        capacidad.crear_repositorio(
            settings.aws_region,
            settings.metrics_namespace,
            settings.environment,
            settings.capacidad_cluster,
            settings.capacidad_servicio,
        )
    )


def _respuesta_capacidad(panel: PanelCapacidad) -> CapacidadRespuesta:
    escalado = panel.ultimo_escalado
    return CapacidadRespuesta(
        ambiente=settings.environment,
        servicio=panel.servicio,
        ventana_min=panel.ventana_min,
        cotizaciones_por_minuto=[
            PuntoCapacidadRespuesta(instante=p.instante, valor=p.valor)
            for p in panel.cotizaciones_por_minuto
        ],
        tareas_activas=panel.tareas_activas,
        minimo=panel.minimo,
        maximo=panel.maximo,
        ultimo_escalado=EscaladoRespuesta(**vars(escalado)) if escalado else None,
    )


@router_capacidad.get(
    "/capacidad",
    response_model=CapacidadRespuesta,
    dependencies=[Depends(requerir_grupo(settings.grupo_operacion))],
)
async def obtener_capacidad(
    caso: Annotated[ConsultarCapacidad | None, Depends(consultar_capacidad)],
) -> CapacidadRespuesta:
    try:
        if caso is None:
            raise CapacidadNoConfigurada
        # boto3 es bloqueante: se ejecuta fuera del event loop.
        panel = await asyncio.to_thread(caso.ejecutar)
    except Exception as error:  # sin configuración, CloudWatch o Auto Scaling no disponibles
        # Solo el tipo: el mensaje de AWS puede traer cuentas, ARN o nombres de recursos.
        logger.error("no se pudo leer la capacidad", extra={"error_type": type(error).__name__})
        raise ErrorNegocio(
            "SERVICIO_NO_DISPONIBLE", "No se pudo leer la capacidad del servicio", 503
        ) from error
    return _respuesta_capacidad(panel)


# Después de declarar sus rutas: include_router copia las que existen (y conserva RutaB2).
router.include_router(router_capacidad)
