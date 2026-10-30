import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
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
from solventa_common.inbox import ConsumidorInbox, InboxPg, Receptor, ReceptorSqs, cliente_sqs
from solventa_common.outbox import OutboxPg, PublicadorSns, RelayOutbox, cliente_sns
from solventa_common.telemetry import instrumentar

from polizas.adapters.inbound.http import aceptaciones, recaudos, suscripciones
from polizas.adapters.inbound.sqs.recaudos import manejador_recaudos
from polizas.adapters.outbound.catalogo_http import CatalogoHttp
from polizas.adapters.outbound.cotizacion_http import CotizacionHttp
from polizas.adapters.outbound.polizas_pg import PolizasPg
from polizas.adapters.outbound.recaudos_pg import RecaudosPg
from polizas.adapters.outbound.reloj import RelojSistema
from polizas.application.aceptacion.consultar import ConsultarAceptacion
from polizas.application.aceptacion.puertos import AceptacionesRepositorio
from polizas.application.aceptacion.registrar import ConfigAceptacion, RegistrarAceptacion
from polizas.application.recaudo.consultar import ConsultarRecaudo
from polizas.application.recaudo.proyectar import ProyectarRecaudo
from polizas.application.recaudo.puertos import RecaudosRepositorio
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

# Al cerrar, en serie: primero el consumidor de recaudos (HU-W16, 16.13) y después el relay; cada
# uno se espera hasta su límite y luego se cancela. Después se cierran el pool y el cliente HTTP.
# ECS da 30 s entre SIGTERM y SIGKILL (stopTimeout, infra/apps/ecs.tf:141); antes del lifespan
# uvicorn termina las solicitudes en curso.
# - Consumidor, 15 s: la parada se atiende entre ciclos; un ciclo es una espera larga (10 s) más el
#   lote (5 transacciones cortas, ms). Uno cortado hace rollback y el mensaje vuelve tras la
#   visibilidad; el inbox evita el doble efecto.
# - Relay, 10 s: un ciclo normal (lote 50, unos ms por publish) cabe de sobra; uno cortado hace
#   rollback y el siguiente relay republica sus filas con el mismo eventId (al menos una vez; un
#   publish ya en vuelo puede llegar igual).
# Primero el consumidor: desde HU-W17 su efecto escribirá eventos en el outbox, y el relay los
# publica en su último ciclo. 15 + 10 = 25 s, con 5 s de margen para uvicorn y los cierres.
PARADA_CONSUMIDOR_S = 15.0
PARADA_RELAY_S = 10.0
MENSAJE_RELAY_SIN_CONFIGURACION = "outbox: relay habilitado sin configuración; no se publica"
MENSAJE_RELAY_NO_ARRANCO = "outbox: el relay no arrancó"
MENSAJE_RELAY_DETENIDO = "outbox: el relay se detuvo"
MENSAJE_RELAY_SIN_PARAR = "outbox: el relay no se detuvo a tiempo; se cancela"
MENSAJE_CONSUMIDOR_SIN_COLA = "inbox: sin cola de recaudos; no se proyecta el recaudo"
MENSAJE_CONSUMIDOR_SIN_CONFIGURACION = "inbox: consumidor sin configuración; no se proyecta"
MENSAJE_CONSUMIDOR_NO_ARRANCO = "inbox: el consumidor no arrancó"
MENSAJE_CONSUMIDOR_DETENIDO = "inbox: el consumidor se detuvo"
MENSAJE_CONSUMIDOR_SIN_PARAR = "inbox: el consumidor no se detuvo a tiempo; se cancela"

logger = logging.getLogger("polizas.outbox")
logger_inbox = logging.getLogger("polizas.inbox")

Ejecutar = Callable[[asyncio.Event], Awaitable[None]]


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
    # HU-W16 (16.13): la lectura del recaudo. Sin él (arneses anteriores, como la R3 de dinero),
    # GET …/recaudo responde 503 DEPENDENCIA_NO_DISPONIBLE.
    recaudos: RecaudosRepositorio | None = None


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
    # HU-W16 (16.13): el estado del recaudo, desde la proyección de polizas-recaudos.
    if puertos.recaudos is not None:
        app.state.consultar_recaudo = ConsultarRecaudo(
            puertos.aceptaciones, puertos.decisiones, puertos.recaudos
        )


@asynccontextmanager
async def _en_fondo(
    app: FastAPI,
    atributo: str,
    ejecutar: Ejecutar,
    *,
    limite_s: float,
    registro: logging.Logger,
    detenido: str,
    sin_parar: str,
) -> AsyncIterator[None]:
    """Corre `ejecutar` como tarea (`app.state.<atributo>`) mientras dura el contexto. Al cerrar,
    señaliza la parada y espera a la tarea hasta `limite_s`; si no termina, se cancela."""

    async def correr(parada: asyncio.Event) -> None:
        # Las librerías ya absorben los ciclos fallidos; esto solo evita que un error inesperado
        # quede como excepción no recuperada de la tarea. La aplicación sigue sirviendo.
        try:
            await ejecutar(parada)
        except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
            registro.error(detenido, extra={"error_type": type(error).__name__})

    parada = asyncio.Event()
    tarea = asyncio.create_task(correr(parada), name=atributo.replace("_", "-"))
    setattr(app.state, atributo, tarea)
    try:
        yield
    finally:
        parada.set()
        try:
            await asyncio.wait_for(tarea, limite_s)
        except TimeoutError:
            registro.warning(sin_parar, extra={"limit_s": limite_s})


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
    async with _en_fondo(
        app,
        "relay_outbox",
        relay.ejecutar,
        limite_s=PARADA_RELAY_S,
        registro=logger,
        detenido=MENSAJE_RELAY_DETENIDO,
        sin_parar=MENSAJE_RELAY_SIN_PARAR,
    ):
        yield


def armar_consumidor(
    pool: "AsyncConnectionPool[Any]", receptor: Receptor, reloj: Reloj
) -> ConsumidorInbox[Any]:
    """El consumidor de `polizas-recaudos` (modo a): inbox y proyección sobre el mismo pool, en
    una transacción por mensaje. HU-W17 (17.6): en esa transacción se crea la póliza."""
    caso = ProyectarRecaudo(RecaudosPg(pool), PolizasPg(), reloj)
    return ConsumidorInbox(receptor, InboxPg(pool), manejador_recaudos(caso), reloj=reloj.ahora)


@asynccontextmanager
async def _consumidor_recaudos(
    app: FastAPI, settings: Settings, reloj: Reloj
) -> AsyncIterator[None]:
    """HU-W16 (16.13): consume `polizas-recaudos` y proyecta el estado del recaudo, sobre el pool.

    Activo solo si llega la cola (en AWS siempre; en local y pruebas, no). Sin base, o si no se
    puede crear el cliente SQS, lo registra y la app arranca igual. Al cerrar, señaliza la parada y
    espera a la tarea hasta `PARADA_CONSUMIDOR_S`.
    """
    cola = settings.sqs_polizas_recaudos_url
    if not cola:
        logger_inbox.info(
            MENSAJE_CONSUMIDOR_SIN_COLA, extra={"missing": "SQS_POLIZAS_RECAUDOS_URL"}
        )
        yield
        return
    pool = getattr(app.state, "pool", None)
    if pool is None:
        logger_inbox.error(MENSAJE_CONSUMIDOR_SIN_CONFIGURACION, extra={"missing": "base"})
        yield
        return
    espera = settings.consumidor_espera_larga_s
    try:
        receptor = ReceptorSqs(
            cliente_sqs(
                settings.aws_region, endpoint_url=settings.aws_endpoint_url, espera_larga_s=espera
            ),
            cola,
            espera_larga_s=espera,
            max_mensajes=settings.consumidor_max_mensajes,
        )
    except Exception as error:  # noqa: BLE001 - el mensaje puede traer la configuración
        logger_inbox.error(
            MENSAJE_CONSUMIDOR_NO_ARRANCO, extra={"error_type": type(error).__name__}
        )
        yield
        return
    consumidor = armar_consumidor(pool, receptor, reloj)
    async with _en_fondo(
        app,
        "consumidor_recaudos",
        consumidor.ejecutar,
        limite_s=PARADA_CONSUMIDOR_S,
        registro=logger_inbox,
        detenido=MENSAJE_CONSUMIDOR_DETENIDO,
        sin_parar=MENSAJE_CONSUMIDOR_SIN_PARAR,
    ):
        yield


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
                        recaudos=RecaudosPg(pool_base),
                    ),
                    settings,
                )
            # Lo último en entrar: al cerrar (en orden inverso), primero el consumidor, luego el
            # relay y después el pool.
            await pila.enter_async_context(_relay_outbox(app, settings, reloj))
            await pila.enter_async_context(_consumidor_recaudos(app, settings, reloj))
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    # HU-W13: suscripción automática; HU-W15: aceptación; HU-W16: estado del recaudo. HU-W17
    # agregará la emisión.
    app.include_router(suscripciones.router)
    app.include_router(aceptaciones.router)
    app.include_router(recaudos.router)

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
