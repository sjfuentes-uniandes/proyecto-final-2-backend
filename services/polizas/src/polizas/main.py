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
from polizas.adapters.outbound.documento_pdf import GeneradorPdf
from polizas.adapters.outbound.documentos_s3 import almacen_desde
from polizas.adapters.outbound.firma_http import AdaptadorFirmaHttp
from polizas.adapters.outbound.outbox_pg import OutboxPolizasPg
from polizas.adapters.outbound.polizas_pg import PolizasPg, TransaccionesPg
from polizas.adapters.outbound.recaudos_pg import RecaudosPg
from polizas.adapters.outbound.reloj import RelojSistema
from polizas.application.aceptacion.consultar import ConsultarAceptacion
from polizas.application.aceptacion.puertos import AceptacionesRepositorio
from polizas.application.aceptacion.registrar import ConfigAceptacion, RegistrarAceptacion
from polizas.application.poliza.documento import ReunirDatosDocumento
from polizas.application.poliza.emitir import EmitirPolizas
from polizas.application.poliza.puertos import AlmacenDocumentos
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

# Al cerrar (HU-W17, 17.8), en dos fases, como dinero (16.11); luego se cierran el cliente de
# adaptador-firma, el pool y el cliente HTTP. ECS da 30 s entre SIGTERM y SIGKILL (stopTimeout,
# infra/apps/ecs.tf:141); antes del lifespan uvicorn termina las solicitudes en curso.
# - Fase 1: se señalizan a la vez el consumidor y la tarea emisora, y se espera a los dos en
#   paralelo, cada uno con su límite; dura lo que el mayor. Si un límite vence, se cancela.
#   - Consumidor, 15 s: la parada se atiende entre ciclos; un ciclo es una espera larga (10 s) más
#     el lote (5 transacciones cortas, ms). Uno cortado hace rollback y el mensaje vuelve tras la
#     visibilidad; el inbox evita el doble efecto.
#   - Tarea emisora, 15 s: la parada se atiende entre pólizas; la que está en curso hace una
#     subida a S3 (1 + 5 s por intento) y una firma (6,5 s). Una cortada sigue EMITIENDO con sus
#     puntos de control (una transacción cortada hace rollback); la cancelación libera su reserva
#     al salir y otra tarea la retoma (si no llega a liberarla, al vencer: 60 s).
# - Fase 2: el relay, 10 s, para publicar lo que dejaron en el outbox (la última PolizaEmitida). Un
#   ciclo normal (lote 50, unos ms por publish) cabe de sobra; uno cortado hace rollback y el
#   siguiente relay republica sus filas con el mismo eventId (al menos una vez).
# max(15, 15) + 10 = 25 s, con 5 s de margen para uvicorn y los cierres. En serie serían 40 s.
PARADA_CONSUMIDOR_S = 15.0
PARADA_EMISION_S = 15.0
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
MENSAJE_EMISION_SIN_CONFIGURACION = "emision: tarea emisora sin configuración; no se emiten pólizas"
MENSAJE_EMISION_DETENIDA = "emision: la tarea emisora se detuvo"
MENSAJE_EMISION_SIN_PARAR = "emision: la tarea emisora no se detuvo a tiempo; se cancela"

logger = logging.getLogger("polizas.outbox")
logger_inbox = logging.getLogger("polizas.inbox")
logger_emision = logging.getLogger("polizas.emision")

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


@dataclass(frozen=True)
class _Fondo:
    """Una tarea en segundo plano, con su señal de parada y su límite para detenerse."""

    tarea: "asyncio.Task[None]"
    parada: asyncio.Event
    limite_s: float
    registro: logging.Logger
    sin_parar: str


def _lanzar(
    app: FastAPI,
    atributo: str,
    ejecutar: Ejecutar,
    *,
    limite_s: float,
    registro: logging.Logger,
    detenido: str,
    sin_parar: str,
) -> _Fondo:
    """Corre `ejecutar` como tarea (`app.state.<atributo>`) mientras la app vive."""

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
    return _Fondo(tarea, parada, limite_s, registro, sin_parar)


async def _detener(*fondos: _Fondo | None) -> None:
    """Señaliza la parada a todas a la vez y espera a todas en paralelo, cada una hasta su límite;
    la que no termina a tiempo se cancela."""
    activos = [fondo for fondo in fondos if fondo is not None]
    for fondo in activos:
        fondo.parada.set()

    async def esperar(fondo: _Fondo) -> None:
        try:
            await asyncio.wait_for(fondo.tarea, fondo.limite_s)
        except TimeoutError:
            fondo.registro.warning(fondo.sin_parar, extra={"limit_s": fondo.limite_s})

    await asyncio.gather(*(esperar(fondo) for fondo in activos))


def _relay_outbox(app: FastAPI, settings: Settings, reloj: Reloj) -> _Fondo | None:
    """HU-W16 (16.6, P16-1): vacía el outbox hacia el tópico mientras la app vive, sobre su pool.

    Sin `OUTBOX_ENABLED` no hace nada. Habilitado sin tópico o sin base, o si no se puede crear el
    cliente SNS, lo registra y la app arranca igual. Se detiene en la fase 2, hasta
    `PARADA_RELAY_S`.
    """
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
        registro=logger,
        detenido=MENSAJE_RELAY_DETENIDO,
        sin_parar=MENSAJE_RELAY_SIN_PARAR,
    )


def armar_consumidor(
    pool: "AsyncConnectionPool[Any]", receptor: Receptor, reloj: Reloj
) -> ConsumidorInbox[Any]:
    """El consumidor de `polizas-recaudos` (modo a): inbox y proyección sobre el mismo pool, en
    una transacción por mensaje. HU-W17 (17.6): en esa transacción se crea la póliza."""
    caso = ProyectarRecaudo(RecaudosPg(pool), PolizasPg(), reloj)
    return ConsumidorInbox(receptor, InboxPg(pool), manejador_recaudos(caso), reloj=reloj.ahora)


def _consumidor_recaudos(app: FastAPI, settings: Settings, reloj: Reloj) -> _Fondo | None:
    """HU-W16 (16.13): consume `polizas-recaudos` y proyecta el estado del recaudo, sobre el pool.

    Activo solo si llega la cola (en AWS siempre; en local y pruebas, no). Sin base, o si no se
    puede crear el cliente SQS, lo registra y la app arranca igual. Se detiene en la fase 1, hasta
    `PARADA_CONSUMIDOR_S`.
    """
    cola = settings.sqs_polizas_recaudos_url
    if not cola:
        logger_inbox.info(
            MENSAJE_CONSUMIDOR_SIN_COLA, extra={"missing": "SQS_POLIZAS_RECAUDOS_URL"}
        )
        return None
    pool = getattr(app.state, "pool", None)
    if pool is None:
        logger_inbox.error(MENSAJE_CONSUMIDOR_SIN_CONFIGURACION, extra={"missing": "base"})
        return None
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
        return None
    consumidor = armar_consumidor(pool, receptor, reloj)
    return _lanzar(
        app,
        "consumidor_recaudos",
        consumidor.ejecutar,
        limite_s=PARADA_CONSUMIDOR_S,
        registro=logger_inbox,
        detenido=MENSAJE_CONSUMIDOR_DETENIDO,
        sin_parar=MENSAJE_CONSUMIDOR_SIN_PARAR,
    )


def armar_emisora(
    pool: "AsyncConnectionPool[Any]",
    almacen: AlmacenDocumentos,
    firma: AdaptadorFirmaHttp,
    reloj: Reloj,
    settings: Settings,
) -> EmitirPolizas[Any]:
    """HU-W17 (17.8): la tarea emisora. Póliza, datos del documento y outbox sobre el mismo pool,
    en transacciones cortas; el PDF, el bucket y la firma, fuera de ellas."""
    polizas = PolizasPg()
    return EmitirPolizas(
        polizas=polizas,
        transacciones=TransaccionesPg(pool),
        datos=ReunirDatosDocumento(polizas),
        generador=GeneradorPdf(),
        almacen=almacen,
        firma=firma,
        eventos=OutboxPolizasPg(),
        reloj=reloj,
        intervalo_s=settings.emision_intervalo_s,
        reintento_s=settings.emision_reintento_s,
        reserva_s=settings.emision_reserva_s,
        lote=settings.emision_lote,
        alerta_tras_s=settings.emision_alerta_tras_s,
    )


@asynccontextmanager
async def _adaptador_firma(app: FastAPI, settings: Settings) -> AsyncIterator[AdaptadorFirmaHttp]:
    """HU-W17 (17.8): el cliente de adaptador-firma de la tarea emisora. Propaga X-Correlation-Id
    y traceparent; cada llamada fija su timeout. Se cierra después de detenerla."""
    async with correlation.cliente_async(timeout=httpx.Timeout(1.0)) as cliente_firma:
        firma = AdaptadorFirmaHttp(
            cliente_firma, settings.adaptador_firma_url, settings.adaptador_firma_timeout_ms
        )
        app.state.cliente_firma = cliente_firma
        app.state.firma = firma
        yield firma


def _emisora(
    app: FastAPI, settings: Settings, reloj: Reloj, firma: AdaptadorFirmaHttp
) -> _Fondo | None:
    """HU-W17 (17.8): emite las pólizas EMITIENDO. Encendida por defecto; sin base, sin almacén o
    sin la URL de adaptador-firma no arranca (WARNING) y la app arranca igual. No depende de la
    cola: las EMITIENDO que quedaron se emiten igual. Se detiene en la fase 1, hasta
    `PARADA_EMISION_S`."""
    if not settings.emision_habilitada:
        return None
    pool = getattr(app.state, "pool", None)
    if pool is None:
        logger_emision.warning(MENSAJE_EMISION_SIN_CONFIGURACION, extra={"missing": "base"})
        return None
    if not settings.adaptador_firma_url.strip():
        logger_emision.warning(
            MENSAJE_EMISION_SIN_CONFIGURACION, extra={"missing": "ADAPTADOR_FIRMA_URL"}
        )
        return None
    almacen = almacen_desde(settings)
    if almacen is None:
        logger_emision.warning(
            MENSAJE_EMISION_SIN_CONFIGURACION, extra={"missing": "POLIZAS_BUCKET"}
        )
        return None
    emisora = armar_emisora(pool, almacen, firma, reloj, settings)
    return _lanzar(
        app,
        "emisora_polizas",
        emisora.ejecutar,
        limite_s=PARADA_EMISION_S,
        registro=logger_emision,
        detenido=MENSAJE_EMISION_DETENIDA,
        sin_parar=MENSAJE_EMISION_SIN_PARAR,
    )


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
            # HU-W17 (17.8): al cerrar, el consumidor y la tarea emisora a la vez, luego el relay;
            # la pila cierra después el cliente de adaptador-firma, el pool y el cliente HTTP.
            firma = await pila.enter_async_context(_adaptador_firma(app, settings))
            relay: _Fondo | None = None
            productores: list[_Fondo | None] = []
            try:
                relay = _relay_outbox(app, settings, reloj)
                productores.append(_consumidor_recaudos(app, settings, reloj))
                productores.append(_emisora(app, settings, reloj, firma))
                yield
            finally:
                await _detener(*productores)
                await _detener(relay)

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    # HU-W13: suscripción automática; HU-W15: aceptación; HU-W16: estado del recaudo. HU-W17
    # (17.9) agregará la lectura de la póliza.
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
