"""GET /operacion/trazas, /operacion/alertas, /operacion/continuidad y
/operacion/continuidad/conciliacion para el back-office (grupo operacion).

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
from pydantic import BaseModel, ValidationError
from solventa_common.correlation import es_valido

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.socios import api_socios
from bff_web.adapters.outbound import cloudwatch, continuidad_aws
from bff_web.adapters.outbound.api_socios import ApiSocios
from bff_web.adapters.outbound.xray import crear_repositorio
from bff_web.application.consultar_alertas import ConsultarAlertas
from bff_web.application.consultar_continuidad import ConsultarConciliacion, ConsultarContinuidad
from bff_web.application.consultar_traza import ConsultarTraza
from bff_web.config import settings
from bff_web.domain.alertas import AlertaAbierta, PanelAlertas, leer_catalogo, leer_umbrales
from bff_web.domain.continuidad import Continuidad
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


# --- HU-W29: Operación › Continuidad y Conciliación ---------------------------------------
class ZonaRespuesta(BaseModel):
    zona: str
    tareas_saludables: int
    tareas_esperadas: int
    trafico_pct: float | None
    estado: Literal["SALUDABLE", "DEGRADADA", "RETIRADA", "NO_DISPONIBLE"]


class BaseRespuesta(BaseModel):
    estado: Literal["MULTI_AZ", "FAILOVER_EN_CURSO", "STANDBY_PROMOVIDO"]
    desde: datetime | None


class RecorridoRespuesta(BaseModel):
    id: str
    nombre: str
    estado: Literal["OPERANDO", "DEGRADADO"]
    alarmas_abiertas: list[str]


class HitoRespuesta(BaseModel):
    instante: datetime
    fuente: Literal["FIS", "ECS", "RDS", "ALARMA"]
    tipo: Literal[
        "INYECCION",
        "FIN_INYECCION",
        "TAREAS_DETENIDAS",
        "TAREAS_INICIADAS",
        "FAILOVER_INICIADO",
        "FAILOVER_COMPLETADO",
        "EVENTO_BASE",
        "ALARMA",
        "RECUPERADA",
    ]
    descripcion: str
    zona: str | None
    cantidad: int | None


class IndicadoresRespuesta(BaseModel):
    experimento: str
    estado_conciliacion: str
    transacciones_perdidas: int
    cobros_duplicados: int
    redireccion_s: int | None
    objetivo_redireccion_s: int | None
    porcentaje_error: float


class ContinuidadRespuesta(BaseModel):
    ambiente: str
    estado: Literal["DOS_ZONAS", "UNA_ZONA", "SIN_DATOS"]
    zonas: list[ZonaRespuesta]
    base: BaseRespuesta
    recorridos: list[RecorridoRespuesta]
    cronologia: list[HitoRespuesta]
    incidente_desde: datetime | None
    indicadores: IndicadoresRespuesta | None


# El informe se devuelve tal como lo genera experiments/hu-w29/reconciliar.py (camelCase): el
# botón "Exportar informe" de I05 descarga ese mismo JSON.
class VerificacionInforme(BaseModel):
    id: str
    descripcion: str
    esperado: int
    observado: int
    resultado: Literal["COINCIDE", "DIFERENCIA", "PENDIENTE"]
    detalle: list[dict[str, Any]]


class VentanaInforme(BaseModel):
    desde: str
    hasta: str


class ResumenInforme(BaseModel):
    # Nombres del informe (camelCase), como los escribe reconciliar.py.
    confirmadas: int
    perdidas: int
    duplicadas: int
    cobrosDuplicados: int
    dlqSinExplicacion: int
    pendientesOutbox: int
    solicitudes: int
    solicitudesConError: int
    porcentajeError: float


class InformeConciliacion(BaseModel):
    version: Literal[1]
    experimento: str
    generado: str
    ventana: VentanaInforme
    estado: Literal["SIN_DIFERENCIAS", "CON_DIFERENCIAS", "PENDIENTE"]
    resumen: ResumenInforme
    verificaciones: list[VerificacionInforme]
    recorridos: dict[str, dict[str, Any]]
    continuidad: dict[str, Any] | None = None
    dlq: dict[str, Any]


@lru_cache
def _repositorios_continuidad() -> tuple[Any, Any, Any]:
    return continuidad_aws.crear_repositorios(
        settings.aws_region,
        settings.continuidad_eventos,
        settings.continuidad_alb,
        settings.continuidad_bucket,
    )


@lru_cache
def consultar_continuidad() -> ConsultarContinuidad:
    eventos, trafico, informes = _repositorios_continuidad()
    return ConsultarContinuidad(
        eventos,
        trafico,
        cloudwatch.crear_repositorio(settings.aws_region, settings.environment),
        informes,
        settings.alertas_prefijo,
        settings.continuidad_recorridos,
        {nombre: str(e.get("titulo", nombre)) for nombre, e in settings.alertas_catalogo.items()},
    )


@lru_cache
def consultar_conciliacion() -> ConsultarConciliacion:
    return ConsultarConciliacion(_repositorios_continuidad()[2])


def _respuesta_continuidad(c: Continuidad) -> ContinuidadRespuesta:
    return ContinuidadRespuesta(
        ambiente=settings.environment,
        estado=c.estado,
        zonas=[ZonaRespuesta(**vars(z)) for z in c.zonas],
        base=BaseRespuesta(**vars(c.base)),
        recorridos=[RecorridoRespuesta(**vars(r)) for r in c.recorridos],
        cronologia=[HitoRespuesta(**vars(h)) for h in c.cronologia],
        incidente_desde=c.incidente_desde,
        indicadores=IndicadoresRespuesta(**vars(c.indicadores)) if c.indicadores else None,
    )


@router.get(
    "/continuidad",
    response_model=ContinuidadRespuesta,
    dependencies=[Depends(requerir_grupo(settings.grupo_operacion))],
)
async def obtener_continuidad(
    caso: Annotated[ConsultarContinuidad, Depends(consultar_continuidad)],
    horas: Annotated[int, Query(ge=1, le=24)] = 6,
) -> ContinuidadRespuesta:
    try:
        continuidad = await asyncio.to_thread(caso.ejecutar, horas)
    except Exception as error:  # CloudWatch no disponible, sin permisos o consulta vencida
        logger.error("no se pudo leer la continuidad", extra={"error_type": type(error).__name__})
        raise HTTPException(502, "No se pudo leer el estado de continuidad del ambiente") from error
    return _respuesta_continuidad(continuidad)


@router.get(
    "/continuidad/conciliacion",
    response_model=InformeConciliacion,
    dependencies=[Depends(requerir_grupo(settings.grupo_operacion))],
)
async def obtener_conciliacion(
    caso: Annotated[ConsultarConciliacion, Depends(consultar_conciliacion)],
) -> Any:
    try:
        informe = await asyncio.to_thread(caso.ejecutar)
    except Exception as error:  # S3 no disponible, sin permisos o JSON ilegible
        logger.error(
            "no se pudo leer el informe de conciliación",
            extra={"error_type": type(error).__name__},
        )
        raise HTTPException(502, "No se pudo leer el informe de conciliación") from error
    if informe is None:
        raise HTTPException(404, "No hay informe de conciliación publicado en este ambiente")
    try:
        return InformeConciliacion.model_validate(informe)
    except ValidationError as error:
        logger.error(
            "informe de conciliación con otro formato", extra={"error_type": "ValidationError"}
        )
        raise HTTPException(
            502, "El informe de conciliación no tiene el formato esperado"
        ) from error
