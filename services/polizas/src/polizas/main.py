import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from solventa_common import correlation
from solventa_common.errors import registrar_manejadores
from solventa_common.outbox import OutboxPg, PublicadorSns, RelayOutbox, cliente_sns
from solventa_common.telemetry import instrumentar

from polizas.adapters.inbound.http import aceptaciones, suscripciones
from polizas.adapters.outbound.catalogo_http import CatalogoHttp
from polizas.adapters.outbound.cotizacion_http import CotizacionHttp
from polizas.adapters.outbound.reloj import RelojSistema
from polizas.application.aceptacion.consultar import ConsultarAceptacion
from polizas.application.aceptacion.puertos import AceptacionesRepositorio
from polizas.application.aceptacion.registrar import ConfigAceptacion, RegistrarAceptacion
from polizas.application.suscripcion.comunicar import ComunicarDecision
from polizas.application.suscripcion.consultar import ConsultarSuscripciones
from polizas.application.suscripcion.puertos import (
    DecisionesRepositorio,
    GeneradorCodigos,
    InsumosOfertaPuerto,
    ReglasSuscripcionPuerto,
    Reloj,
)
from polizas.application.suscripcion.reconstruir import ReconstruirDecision
from polizas.application.suscripcion.solicitar_suscripcion import (
    ConfigSuscripcion,
    SolicitarSuscripcion,
)
from polizas.config import Settings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# Al cerrar, se espera al relay hasta este límite y luego se cancela. ECS da 30 s entre SIGTERM y
# SIGKILL (stopTimeout, infra/apps/ecs.tf); antes del lifespan uvicorn termina las solicitudes en
# curso y después se cierra el pool. Un ciclo normal (lote 50, unos ms por publish) cabe de sobra;
# uno cortado hace rollback y el siguiente relay republica sus filas con el mismo eventId (al menos
# una vez; un publish ya en vuelo puede llegar igual).
PARADA_RELAY_S = 10.0
MENSAJE_RELAY_SIN_CONFIGURACION = "outbox: relay habilitado sin configuración; no se publica"
MENSAJE_RELAY_NO_ARRANCO = "outbox: el relay no arrancó"
MENSAJE_RELAY_DETENIDO = "outbox: el relay se detuvo"
MENSAJE_RELAY_SIN_PARAR = "outbox: el relay no se detuvo a tiempo; se cancela"

logger = logging.getLogger("polizas.outbox")


@dataclass(frozen=True, slots=True)
class Puertos:
    """Implementaciones de los puertos de la suscripción y la aceptación; las pruebas inyectan
    dobles."""

    insumos: InsumosOfertaPuerto
    reglas: ReglasSuscripcionPuerto
    decisiones: DecisionesRepositorio
    codigos: GeneradorCodigos
    reloj: Reloj
    aceptaciones: AceptacionesRepositorio


def _configurar_casos(app: FastAPI, puertos: Puertos, settings: Settings) -> None:
    app.state.solicitar_suscripcion = SolicitarSuscripcion(
        insumos=puertos.insumos,
        reglas=puertos.reglas,
        repo=puertos.decisiones,
        codigos=puertos.codigos,
        reloj=puertos.reloj,
        config=ConfigSuscripcion(
            cotizacion_timeout_ms=settings.cotizacion_timeout_ms,
            catalogo_timeout_ms=settings.catalogo_timeout_ms,
        ),
    )
    app.state.consultar_suscripciones = ConsultarSuscripciones(
        puertos.decisiones, puertos.reloj, settings.compromiso_revision_horas
    )
    app.state.reconstruir_decision = ReconstruirDecision(
        puertos.decisiones, puertos.reglas, settings.catalogo_timeout_ms
    )
    # HU-W14: la comunicación se deriva al leer (GET y POST); no se persiste.
    app.state.comunicar_decision = ComunicarDecision(
        puertos.reglas,
        puertos.reloj,
        settings.compromiso_revision_horas,
        settings.catalogo_timeout_ms,
    )
    # HU-W15: sin llamadas a otros servicios; todo está en la instantánea de la decisión (DS2-10).
    app.state.registrar_aceptacion = RegistrarAceptacion(
        puertos.decisiones,
        puertos.aceptaciones,
        puertos.reloj,
        ConfigAceptacion(version_declaraciones=settings.version_declaraciones),
    )
    app.state.consultar_aceptacion = ConsultarAceptacion(puertos.aceptaciones)


async def _ejecutar_relay(relay: RelayOutbox, parada: asyncio.Event) -> None:
    # La librería ya absorbe los ciclos fallidos; esto solo evita que un error inesperado quede
    # como excepción no recuperada de la tarea. La aplicación sigue sirviendo.
    try:
        await relay.ejecutar(parada)
    except Exception as error:  # noqa: BLE001
        logger.error(MENSAJE_RELAY_DETENIDO, extra={"error_type": type(error).__name__})


@asynccontextmanager
async def _relay_outbox(app: FastAPI, settings: Settings, reloj: Reloj) -> AsyncIterator[None]:
    """HU-W16 (16.6, P16-1): vacía el outbox hacia el tópico mientras la app vive, sobre su pool.

    Sin `OUTBOX_ENABLED` no hace nada. Habilitado sin tópico o sin base, o si no se puede crear el
    cliente SNS, lo registra y la app arranca igual. Al cerrar, señaliza la parada y espera a la
    tarea hasta `PARADA_RELAY_S`.
    """
    if not settings.outbox_enabled:
        yield
        return
    topico = settings.events_topic_arn
    pool = getattr(app.state, "pool", None)
    if not topico or pool is None:
        falta = "EVENTS_TOPIC_ARN" if not topico else "base"
        logger.error(MENSAJE_RELAY_SIN_CONFIGURACION, extra={"missing": falta})
        yield
        return
    try:
        cliente = cliente_sns(settings.aws_region, endpoint_url=settings.aws_endpoint_url)
    except Exception as error:  # noqa: BLE001 - el mensaje puede traer la configuración
        logger.error(MENSAJE_RELAY_NO_ARRANCO, extra={"error_type": type(error).__name__})
        yield
        return
    relay = RelayOutbox(
        OutboxPg(pool),
        PublicadorSns(cliente, topico),
        lote=settings.outbox_lote,
        max_intentos=settings.outbox_max_intentos,
        intervalo_s=settings.outbox_intervalo_s,
        reloj=reloj.ahora,
    )
    parada = asyncio.Event()
    tarea = asyncio.create_task(_ejecutar_relay(relay, parada), name="relay-outbox")
    app.state.relay_outbox = tarea
    try:
        yield
    finally:
        parada.set()
        try:
            await asyncio.wait_for(tarea, PARADA_RELAY_S)
        except TimeoutError:
            logger.warning(MENSAJE_RELAY_SIN_PARAR, extra={"limit_s": PARADA_RELAY_S})


def crear_app(
    settings: Settings | None = None,
    *,
    puertos: Puertos | None = None,
    pool: "AsyncConnectionPool[Any] | None" = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de polizas. Las pruebas inyectan los puertos y los exportadores; con los puertos, también
    el pool sobre el que corren (el del relay y /health)."""
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as pila:
            reloj: Reloj = puertos.reloj if puertos is not None else RelojSistema()
            if puertos is not None:
                _configurar_casos(app, puertos, settings)
                if pool is not None:
                    app.state.pool = pool
            elif settings.db_host or settings.database_url_override:
                from psycopg_pool import AsyncConnectionPool

                from polizas.adapters.outbound.aceptaciones_pg import AceptacionesPg
                from polizas.adapters.outbound.codigos import CodigosPg
                from polizas.adapters.outbound.decisiones_pg import DecisionesPg, migrar

                migrar(settings.database_url)
                pool_base = AsyncConnectionPool(
                    settings.database_url,
                    min_size=1,
                    max_size=settings.db_pool_size,
                    open=False,
                )
                await pool_base.open()
                pila.push_async_callback(pool_base.close)
                app.state.pool = pool_base
                # Un cliente para las dos dependencias; propaga X-Correlation-Id y traceparent.
                # Cada llamada fija su propio timeout (DS2-19).
                cliente = await pila.enter_async_context(
                    correlation.cliente_async(
                        timeout=httpx.Timeout(1.0), limits=httpx.Limits(max_connections=100)
                    )
                )
                _configurar_casos(
                    app,
                    Puertos(
                        insumos=CotizacionHttp(
                            cliente, settings.cotizacion_url, settings.cotizacion_timeout_ms
                        ),
                        reglas=CatalogoHttp(
                            cliente,
                            settings.catalogo_url,
                            settings.catalogo_timeout_ms,
                            reloj,
                            settings.reglas_cache_vigente_s,
                            settings.reglas_cache_version_s,
                        ),
                        decisiones=DecisionesPg(pool_base),
                        codigos=CodigosPg(pool_base),
                        reloj=reloj,
                        aceptaciones=AceptacionesPg(pool_base),
                    ),
                    settings,
                )
            # Lo último en entrar: al cerrar, el relay se detiene antes que el pool.
            await pila.enter_async_context(_relay_outbox(app, settings, reloj))
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    # HU-W13: suscripción automática; HU-W15: aceptación. HU-W17 agregará la emisión.
    app.include_router(suscripciones.router)
    app.include_router(aceptaciones.router)

    @app.get("/health", response_model=None)
    async def health(request: Request) -> dict[str, str] | JSONResponse:
        pool = getattr(request.app.state, "pool", None)
        if pool is not None:
            try:
                async with pool.connection() as conexion:
                    await conexion.execute("SELECT 1")
            except (psycopg.Error, OSError):
                return JSONResponse({"status": "base no disponible"}, status_code=503)
        return {"status": "ok"}

    return app


app = crear_app()
