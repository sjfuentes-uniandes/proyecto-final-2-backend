import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from solventa_common.outbox import OutboxPg, PublicadorSns, RelayOutbox, cliente_sns
from solventa_common.telemetry import instrumentar

from siniestros.adapters.inbound.http import decisiones
from siniestros.adapters.inbound.http import siniestros as http
from siniestros.adapters.outbound.base_de_datos import migrar
from siniestros.adapters.outbound.evidencias_s3 import (
    AlmacenSinConfigurar,
    EvidenciasS3,
    cliente_s3,
)
from siniestros.adapters.outbound.reloj import RelojSistema
from siniestros.adapters.outbound.semilla_qa import sembrar_con_reintentos
from siniestros.adapters.outbound.siniestros_pg import SiniestrosPg
from siniestros.adapters.outbound.soportes_requeridos import SoportesRequeridosJson
from siniestros.application.siniestro.acceso import RegistrarAccesoDenegado
from siniestros.application.siniestro.bandeja import ConsultarResumen, ListarBandeja
from siniestros.application.siniestro.decidir import Decidir
from siniestros.application.siniestro.detalle import ConsultarDetalle
from siniestros.application.siniestro.enlace import EmitirEnlace
from siniestros.application.siniestro.plazos import VencerPlazosSoporte
from siniestros.application.siniestro.puertos import AlmacenEvidencias, Reloj
from siniestros.application.siniestro.tomar import TomarSiguiente
from siniestros.application.siniestro.vistas import Plazos
from siniestros.config import Settings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# Al cerrar: el relay del Outbox publica lo último que quedó escrito. ECS da 30 s entre SIGTERM y
# SIGKILL (stopTimeout, infra/apps/ecs.tf); un ciclo normal tarda milisegundos.
PARADA_RELAY_S = 5.0

MENSAJE_RELAY_SIN_CONFIGURACION = "outbox: relay habilitado sin configuración; no se publica"
MENSAJE_RELAY_NO_ARRANCO = "outbox: el relay no arrancó"
MENSAJE_RELAY_DETENIDO = "outbox: el relay se detuvo"
MENSAJE_RELAY_SIN_PARAR = "outbox: el relay no se detuvo a tiempo; se cancela"
MENSAJE_SEMILLA_EN_PRODUCCION = "semilla de QA: está encendida en producción; no se aplica"
MENSAJE_SEMILLA_DETENIDA = "semilla de QA: se detuvo"
MENSAJE_SEMILLA_SIN_PARAR = "semilla de QA: no se detuvo a tiempo; se cancela"
PARADA_SEMILLA_S = 5.0
MENSAJE_PLAZOS_SIN_BASE = "plazos: sin base; no se vencen los plazos de soporte"
MENSAJE_PLAZOS_DETENIDOS = "plazos: la tarea se detuvo"
MENSAJE_PLAZOS_SIN_PARAR = "plazos: la tarea no se detuvo a tiempo; se cancela"
# Una pasada termina con su lote en curso: cabe en el plazo de ECS (30 s) con holgura.
PARADA_PLAZOS_S = 10.0
# Cada PutObject de la semilla sube un archivo pequeño: más holgado que el corte de las lecturas.
SEMILLA_TIMEOUT_S_MS = 5000
AMBIENTES_SIN_SEMILLA = frozenset({"prod", "production", "produccion"})
MENSAJE_SIN_BUCKET = "evidencias: sin bucket; no se verifican ni se firman archivos"

logger = logging.getLogger("siniestros.ciclo")

Ejecutar = Callable[[asyncio.Event], Awaitable[None]]


def armar_servicios(
    pool: "AsyncConnectionPool[Any]", almacen: AlmacenEvidencias, reloj: Reloj, settings: Settings
) -> http.Servicios:
    """Los casos de uso de HU-W19 sobre un pool, un almacén y un reloj."""
    repo = SiniestrosPg(pool)
    plazos = Plazos(
        settings.compromiso_horas, settings.prioridad_alta_min, settings.prioridad_media_min
    )
    return http.Servicios(
        bandeja=ListarBandeja(repo, reloj, plazos),
        resumen=ConsultarResumen(repo, reloj, plazos),
        tomar=TomarSiguiente(repo, reloj, plazos),
        detalle=ConsultarDetalle(
            repo,
            almacen,
            SoportesRequeridosJson.cargar(),
            reloj,
            plazos,
            settings.coherencia_distancia_max_m,
        ),
        enlace=EmitirEnlace(repo, almacen, reloj, settings.evidencia_url_ttl_s),
        decidir=Decidir(repo, reloj),
        acceso=RegistrarAccesoDenegado(repo, reloj),
        reloj=reloj,
        plazos=plazos,
    )


def armar_almacen(settings: Settings) -> AlmacenEvidencias:
    """S3 con corte y sin reintentos; sin bucket, un almacén que no verifica ni firma."""
    if not settings.evidencias_bucket:
        logger.warning(MENSAJE_SIN_BUCKET, extra={"missing": "EVIDENCIAS_BUCKET"})
        return AlmacenSinConfigurar()
    cliente = cliente_s3(settings.aws_region, settings.s3_timeout_ms, settings.aws_endpoint_url)
    # En local las URL se firman con el endpoint que el navegador alcanza, no el de compose.
    firma = (
        cliente_s3(
            settings.aws_region, settings.s3_timeout_ms, settings.evidencias_endpoint_publico
        )
        if settings.evidencias_endpoint_publico
        else cliente
    )
    return EvidenciasS3(cliente, settings.evidencias_bucket, settings.s3_timeout_ms, firma)


@dataclass(frozen=True)
class _Fondo:
    """Una tarea en segundo plano, con su señal de parada y su límite para detenerse."""

    tarea: "asyncio.Task[None]"
    parada: asyncio.Event
    limite_s: float
    sin_parar: str


def _lanzar(
    app: FastAPI,
    atributo: str,
    ejecutar: Ejecutar,
    *,
    limite_s: float,
    detenido: str,
    sin_parar: str,
) -> _Fondo:
    """Corre `ejecutar` como tarea mientras la app vive. Un error inesperado se registra y la app
    sigue sirviendo."""

    async def correr(parada: asyncio.Event) -> None:
        try:
            await ejecutar(parada)
        except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
            logger.error(detenido, extra={"error_type": type(error).__name__})

    parada = asyncio.Event()
    tarea = asyncio.create_task(correr(parada), name=atributo.replace("_", "-"))
    setattr(app.state, atributo, tarea)
    return _Fondo(tarea, parada, limite_s, sin_parar)


async def _detener(fondo: _Fondo | None) -> None:
    """Señaliza la parada y espera hasta el límite; si no termina a tiempo, la cancela."""
    if fondo is None:
        return
    fondo.parada.set()
    try:
        await asyncio.wait_for(fondo.tarea, fondo.limite_s)
    except TimeoutError:
        logger.warning(fondo.sin_parar, extra={"limit_s": fondo.limite_s})


def _relay_outbox(app: FastAPI, settings: Settings, reloj: Reloj) -> _Fondo | None:
    """Vacía el outbox hacia el tópico, sobre el pool. Sin `OUTBOX_ENABLED` no hace nada; sin
    tópico, base o cliente SNS, lo registra y la app arranca igual."""
    if not settings.outbox_enabled:
        return None
    topico = settings.events_topic_arn
    pool = getattr(app.state, "pool", None)
    if not topico or pool is None:
        falta = "EVENTS_TOPIC_ARN" if not topico else "base"
        logger.error(MENSAJE_RELAY_SIN_CONFIGURACION, extra={"missing": falta})
        return None
    try:
        cliente = cliente_sns(settings.aws_region, endpoint_url=settings.aws_endpoint_url)
    except Exception as error:  # noqa: BLE001 - el mensaje puede traer la configuración
        logger.error(MENSAJE_RELAY_NO_ARRANCO, extra={"error_type": type(error).__name__})
        return None
    relay = RelayOutbox(
        OutboxPg(pool),
        PublicadorSns(cliente, topico),
        lote=settings.outbox_lote,
        max_intentos=settings.outbox_max_intentos,
        intervalo_s=settings.outbox_intervalo_s,
        reloj=reloj.ahora,
    )
    return _lanzar(
        app,
        "relay_outbox",
        relay.ejecutar,
        limite_s=PARADA_RELAY_S,
        detenido=MENSAJE_RELAY_DETENIDO,
        sin_parar=MENSAJE_RELAY_SIN_PARAR,
    )


def _plazos(app: FastAPI, settings: Settings, reloj: Reloj) -> _Fondo | None:
    """HU-W20 (DS3-13): devuelve a evaluación los casos con el plazo de soporte vencido. Encendida por
    omisión; sin base no arranca (lo registra) y la app arranca igual."""
    if not settings.plazos_habilitados:
        return None
    pool = getattr(app.state, "pool", None)
    if pool is None:
        logger.warning(MENSAJE_PLAZOS_SIN_BASE, extra={"missing": "base"})
        return None
    tarea = VencerPlazosSoporte(SiniestrosPg(pool), reloj, settings.plazos_lote)

    async def ejecutar(parada: asyncio.Event) -> None:
        await tarea.ejecutar(parada, settings.plazos_intervalo_s)

    return _lanzar(
        app,
        "plazos_soporte",
        ejecutar,
        limite_s=PARADA_PLAZOS_S,
        detenido=MENSAJE_PLAZOS_DETENIDOS,
        sin_parar=MENSAJE_PLAZOS_SIN_PARAR,
    )


def _semilla_qa(app: FastAPI, settings: Settings, reloj: Reloj) -> _Fondo | None:
    """DS3-07: siembra los casos sintéticos de QA. Apagada por omisión; en producción no se aplica
    aunque alguien la encienda."""
    if not settings.semilla_qa:
        return None
    pool = getattr(app.state, "pool", None)
    if settings.environment.lower() in AMBIENTES_SIN_SEMILLA:
        logger.error(MENSAJE_SEMILLA_EN_PRODUCCION)
        return None
    if pool is None:
        logger.error(MENSAJE_RELAY_SIN_CONFIGURACION, extra={"missing": "base"})
        return None
    bucket = settings.evidencias_bucket
    cliente = (
        cliente_s3(settings.aws_region, SEMILLA_TIMEOUT_S_MS, settings.aws_endpoint_url)
        if bucket
        else None
    )

    async def ejecutar(parada: asyncio.Event) -> None:
        await sembrar_con_reintentos(
            parada,
            pool,
            cliente,
            bucket,
            settings.evidencias_bucket_sse,
            reloj,
            settings.semilla_analista_id,
        )

    return _lanzar(
        app,
        "semilla_qa",
        ejecutar,
        limite_s=PARADA_SEMILLA_S,
        detenido=MENSAJE_SEMILLA_DETENIDA,
        sin_parar=MENSAJE_SEMILLA_SIN_PARAR,
    )


def crear_app(
    settings: Settings | None = None,
    *,
    pool: "AsyncConnectionPool[Any] | None" = None,
    almacen: AlmacenEvidencias | None = None,
    reloj: Reloj | None = None,
) -> FastAPI:
    """App de siniestros. Las pruebas inyectan el pool (sobre una base ya migrada; lo abren y lo
    cierran ellas), el almacén y el reloj."""
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        reloj_activo = reloj or RelojSistema()
        async with AsyncExitStack() as pila:
            if pool is not None:
                app.state.pool = pool
            elif settings.db_host or settings.database_url_override:
                from psycopg_pool import AsyncConnectionPool

                migrar(settings.database_url)
                pool_base = AsyncConnectionPool(
                    settings.database_url, min_size=1, max_size=settings.db_pool_size, open=False
                )
                await pool_base.open()
                pila.push_async_callback(pool_base.close)
                app.state.pool = pool_base
            pool_activo = getattr(app.state, "pool", None)
            if pool_activo is not None:
                app.state.servicios = armar_servicios(
                    pool_activo, almacen or armar_almacen(settings), reloj_activo, settings
                )
            relay: _Fondo | None = None
            semilla: _Fondo | None = None
            plazos: _Fondo | None = None
            try:
                relay = _relay_outbox(app, settings, reloj_activo)
                semilla = _semilla_qa(app, settings, reloj_activo)
                plazos = _plazos(app, settings, reloj_activo)
                yield
            finally:
                # Primero lo que escribe en el Outbox; el relay se detiene al final para publicar
                # lo último que quedó escrito.
                await _detener(plazos)
                await _detener(semilla)
                await _detener(relay)

    app = instrumentar(FastAPI(title=settings.service_name, lifespan=ciclo), settings)
    # Primero la decisión: `/catalogos-decision` va antes de `/{siniestroId}`.
    app.include_router(decisiones.router)
    app.include_router(http.router)
    app.include_router(http.router_accesos)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = crear_app()
