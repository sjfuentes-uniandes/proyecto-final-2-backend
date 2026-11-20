import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from solventa_common import correlation
from solventa_common.errors import registrar_manejadores
from solventa_common.inbox import (
    ESPERA_LARGA_S,
    ConsumidorInbox,
    InboxPg,
    ReceptorSqs,
    cliente_sqs,
)
from solventa_common.outbox import OutboxPg, PublicadorSns, RelayOutbox, cliente_sns
from solventa_common.resiliencia import ProteccionDependencia
from solventa_common.telemetry import instrumentar

from pagos_parametricos.adapters.inbound.http import descartes, resumen
from pagos_parametricos.adapters.inbound.sqs.eventos import manejador_eventos, reanudador_eventos
from pagos_parametricos.adapters.inbound.sqs.polizas import manejador_polizas
from pagos_parametricos.adapters.outbound.activaciones_pg import ActivacionesPg
from pagos_parametricos.adapters.outbound.coberturas_pg import CoberturasPg
from pagos_parametricos.adapters.outbound.descartes_pg import (
    RegistroFallosPg,
    ResolucionesPg,
    migrar,
)
from pagos_parametricos.adapters.outbound.dlq_sqs import DlqSqs
from pagos_parametricos.adapters.outbound.evaluaciones_pg import EvaluacionesPg
from pagos_parametricos.adapters.outbound.reloj import RelojSistema
from pagos_parametricos.adapters.outbound.resumen_pg import ResumenPg
from pagos_parametricos.adapters.outbound.siniestros_http import SiniestrosHttp
from pagos_parametricos.adapters.outbound.verificaciones_pg import VerificacionesPg
from pagos_parametricos.application.descartes import ColaDescarte, Dlq, Reloj
from pagos_parametricos.application.parametrico.activar import ActivarSiniestros
from pagos_parametricos.application.parametrico.aplicar_poliza import AplicarEventoPoliza
from pagos_parametricos.application.parametrico.evaluar_evento import EvaluarEvento
from pagos_parametricos.application.parametrico.puertos import Siniestros
from pagos_parametricos.application.parametrico.resumen import ConsultarResumen, ProfundidadDlq
from pagos_parametricos.config import Settings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# Al cerrar, el relay tiene su límite para publicar lo último (un ciclo normal tarda ms; uno cortado
# hace rollback y se republica). ECS da 30 s entre SIGTERM y SIGKILL.
PARADA_RELAY_S = 5.0
# HU-W24 (§9): la visibilidad de las colas `parametricos-*` (catalog.tf) y el plazo de los mensajes
# en curso al parar. Los consumidores paran antes que el relay: lo último que escriban se publica.
VISIBILIDAD_S = 30
PLAZO_PARADA_S = 10.0
PARADA_CONSUMIDOR_S = PLAZO_PARADA_S + 5.0
MENSAJE_CONSUMO_SIN_COLA = "consumo: sin cola o sin base; no se consume"
MENSAJE_CONSUMO_NO_ARRANCO = "consumo: no se pudo crear el cliente SQS"
MENSAJE_CONSUMO_SIN_PARAR = "consumo: el consumidor no se detuvo a tiempo; se cancela"
MENSAJE_DESCARTE_SIN_COLA = "descarte: sin cola de eventos o sin base; la API responde 503"
MENSAJE_DESCARTE_NO_ARRANCO = "descarte: no se pudo crear el cliente SQS"
MENSAJE_RELAY_SIN_CONFIGURACION = "outbox: relay habilitado sin configuración; no se publica"
MENSAJE_RELAY_NO_ARRANCO = "outbox: el relay no arrancó"
MENSAJE_RELAY_DETENIDO = "outbox: el relay se detuvo"
MENSAJE_RELAY_SIN_PARAR = "outbox: el relay no se detuvo a tiempo; se cancela"

logger = logging.getLogger("pagos_parametricos.ciclo")

Ejecutar = Callable[[asyncio.Event], Awaitable[None]]


@asynccontextmanager
async def _en_fondo(
    app: FastAPI,
    atributo: str,
    ejecutar: Ejecutar,
    limite_s: float = PARADA_RELAY_S,
    mensaje_sin_parar: str = MENSAJE_RELAY_SIN_PARAR,
) -> AsyncIterator[None]:
    """Corre `ejecutar` como tarea mientras dura el contexto; al cerrar, la para con su límite."""

    async def correr(parada: asyncio.Event) -> None:
        try:
            await ejecutar(parada)
        except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
            logger.error(MENSAJE_RELAY_DETENIDO, extra={"error_type": type(error).__name__})

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
            logger.warning(mensaje_sin_parar, extra={"limit_s": limite_s})


def armar_cola(
    pool: "AsyncConnectionPool[Any]", dlq: Dlq, reloj: Reloj, settings: Settings
) -> ColaDescarte:
    """HU-W24: la cola de descarte, con la verificación de H11 sobre la copia local (HU-W21)."""
    return ColaDescarte(
        dlq,
        RegistroFallosPg(pool, reloj),
        ResolucionesPg(pool),
        reloj,
        VerificacionesPg(pool, reloj),
        max_mensajes=settings.descarte_max_mensajes,
        max_intentos=settings.descarte_max_intentos,
    )


def _cola_descarte(app: FastAPI, settings: Settings, reloj: Reloj) -> None:
    cola = settings.sqs_parametricos_eventos_url
    pool = getattr(app.state, "pool", None)
    if not cola or pool is None:
        logger.info(MENSAJE_DESCARTE_SIN_COLA)
        return
    try:
        cliente = cliente_sqs(settings.aws_region, endpoint_url=settings.aws_endpoint_url)
    except Exception as error:  # noqa: BLE001 - el mensaje puede traer la configuración
        logger.error(MENSAJE_DESCARTE_NO_ARRANCO, extra={"error_type": type(error).__name__})
        return
    dlq = DlqSqs(cliente, cola, settings.sqs_parametricos_eventos_dlq_url)
    app.state.dlq = dlq
    app.state.cola_descarte = armar_cola(pool, dlq, reloj, settings)


def _resumen(app: FastAPI, reloj: Reloj) -> None:
    """El seguimiento de H09 (HU-W22): lectura de la base y, si hay DLQ configurada (la misma de la
    cola de descarte), su profundidad (null si no se puede leer). Sin base, las rutas dan 503."""
    pool = getattr(app.state, "pool", None)
    if pool is None:
        return
    dlq = getattr(app.state, "dlq", None)
    profundidad: ProfundidadDlq | None = dlq if hasattr(dlq, "profundidad") else None
    app.state.resumen = ConsultarResumen(ResumenPg(pool), profundidad, reloj)


def armar_consumidores(
    pool: "AsyncConnectionPool[Any]",
    cliente: Any,
    reloj: Reloj,
    settings: Settings,
    siniestros: Siniestros | None = None,
    visibilidad_s: int = VISIBILIDAD_S,
    espera_larga_s: int = ESPERA_LARGA_S,
) -> dict[str, ConsumidorInbox[Any]]:
    """Los dos consumidores de HU-W21, con la forma de HU-W24 (§9): `ConsumidorInbox` con
    contrapresión (`SQS_CONCURRENCIA`), latido de visibilidad y parada ordenada. El de pólizas
    escribe su efecto en la transacción del inbox (modo (a)). El de `parametricos-eventos` evalúa en
    esa transacción y devuelve la continuación de HU-W22 (modo (b)): después del commit crea los
    siniestros de las activaciones `PENDIENTE`, y `reanudar` (DS3-15) la retoma si el mensaje vuelve
    con `siniestros` caído; además anota la causa de cada fallo, que luego muestra la DLQ (H10).
    `siniestros` es el puerto que usa la continuación (`SiniestrosHttp` en la app). `visibilidad_s`
    y `espera_larga_s` son los de la cola y el cliente; solo las pruebas de módulo los bajan, para no
    esperar 30 s por recepción ni 20 s al parar."""
    consumidores: dict[str, ConsumidorInbox[Any]] = {}
    if settings.sqs_parametricos_eventos_url:
        evaluar = EvaluarEvento(CoberturasPg(), EvaluacionesPg(), reloj)
        # Sin `siniestros` (pruebas de W21), las activaciones quedan `PENDIENTE` como en el modo (a).
        activar = (
            ActivarSiniestros(ActivacionesPg(pool), siniestros, settings.siniestros_bulkhead_max)
            if siniestros is not None
            else None
        )
        consumidores["consumidor_eventos"] = ConsumidorInbox(
            ReceptorSqs(
                cliente,
                settings.sqs_parametricos_eventos_url,
                espera_larga_s=espera_larga_s,
                max_mensajes=10,
            ),
            InboxPg(pool),
            manejador_eventos(evaluar, activar),
            concurrencia=settings.sqs_concurrencia,
            visibilidad_s=visibilidad_s,
            plazo_parada_s=PLAZO_PARADA_S,
            al_fallar=RegistroFallosPg(pool, reloj).al_fallar,
            reanudar=reanudador_eventos(activar) if activar is not None else None,
        )
    if settings.sqs_parametricos_polizas_url:
        aplicar = AplicarEventoPoliza(CoberturasPg())
        consumidores["consumidor_polizas"] = ConsumidorInbox(
            ReceptorSqs(
                cliente,
                settings.sqs_parametricos_polizas_url,
                espera_larga_s=espera_larga_s,
                max_mensajes=10,
            ),
            InboxPg(pool),
            manejador_polizas(aplicar),
            concurrencia=settings.sqs_concurrencia,
            visibilidad_s=visibilidad_s,
            plazo_parada_s=PLAZO_PARADA_S,
        )
    return consumidores


def armar_siniestros(cliente: httpx.AsyncClient, settings: Settings) -> SiniestrosHttp:
    """La ACL hacia `siniestros` (DS3-23): timeout de 600 ms, un circuito por operación y *bulkhead*
    de 10 (`ProteccionDependencia`). Una sola protección por app."""
    return SiniestrosHttp(
        cliente,
        settings.siniestros_url,
        settings.siniestros_timeout_ms,
        ProteccionDependencia(
            settings.cb_fallos_consecutivos,
            settings.cb_apertura_s,
            settings.siniestros_bulkhead_max,
        ),
    )


@asynccontextmanager
async def _consumidores(
    app: FastAPI, settings: Settings, reloj: Reloj, siniestros: Siniestros | None = None
) -> AsyncIterator[None]:
    """Lanza los consumidores de las colas configuradas y los para con el cierre. Sin base o sin
    ninguna cola, lo registra y la app arranca igual. `siniestros` solo lo inyectan las pruebas; en
    la app es `SiniestrosHttp` con `correlation.cliente_async()`."""
    pool = getattr(app.state, "pool", None)
    if pool is None or not (
        settings.sqs_parametricos_eventos_url or settings.sqs_parametricos_polizas_url
    ):
        logger.info(MENSAJE_CONSUMO_SIN_COLA)
        yield
        return
    try:
        cliente = cliente_sqs(settings.aws_region, endpoint_url=settings.aws_endpoint_url)
    except Exception as error:  # noqa: BLE001 - el mensaje puede traer la configuración
        logger.error(MENSAJE_CONSUMO_NO_ARRANCO, extra={"error_type": type(error).__name__})
        yield
        return
    async with AsyncExitStack() as pila:
        if siniestros is None:
            http = await pila.enter_async_context(correlation.cliente_async())
            siniestros = armar_siniestros(http, settings)
        consumidores = armar_consumidores(pool, cliente, reloj, settings, siniestros)
        for atributo, consumidor in consumidores.items():
            await pila.enter_async_context(
                _en_fondo(
                    app,
                    atributo,
                    consumidor.ejecutar,
                    PARADA_CONSUMIDOR_S,
                    MENSAJE_CONSUMO_SIN_PARAR,
                )
            )
        yield


@asynccontextmanager
async def _relay(app: FastAPI, settings: Settings, reloj: Reloj) -> AsyncIterator[None]:
    """Como dinero: vacía el outbox hacia el tópico, sobre el pool. Sin `OUTBOX_ENABLED` no hace
    nada; sin tópico, base o cliente SNS, lo registra y la app arranca igual."""
    pool = getattr(app.state, "pool", None)
    if not settings.outbox_enabled:
        yield
        return
    if not settings.events_topic_arn or pool is None:
        falta = "EVENTS_TOPIC_ARN" if not settings.events_topic_arn else "base"
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
        PublicadorSns(cliente, settings.events_topic_arn),
        lote=settings.outbox_lote,
        max_intentos=settings.outbox_max_intentos,
        intervalo_s=settings.outbox_intervalo_s,
        reloj=reloj.ahora,
    )
    async with _en_fondo(app, "relay_outbox", relay.ejecutar):
        yield


def crear_app(
    settings: Settings | None = None,
    *,
    reloj: Reloj | None = None,
    pool: "AsyncConnectionPool[Any] | None" = None,
    dlq: Dlq | None = None,
    siniestros: Siniestros | None = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de pagos-parametricos. Las pruebas inyectan el pool, la DLQ y los exportadores.

    HU-W24 deja la cola de descarte y el relay; HU-W21 agrega los consumidores de
    `parametricos-eventos` y `parametricos-polizas`, que se lanzan y paran con el ciclo de vida.
    HU-W22 suma la creación del siniestro (continuación de `parametricos-eventos`, hacia
    `siniestros`) y el seguimiento de H09 (`GET /v1/parametricos/resumen`).
    """
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        reloj_app: Reloj = reloj or RelojSistema()
        async with AsyncExitStack() as pila:
            if pool is not None:
                app.state.pool = pool
            elif settings.db_host or settings.database_url_override:
                from psycopg_pool import AsyncConnectionPool

                migrar(settings.database_url)
                pool_base = AsyncConnectionPool(
                    settings.database_url,
                    min_size=1,
                    max_size=settings.pool_max,
                    open=False,
                )
                await pool_base.open()
                pila.push_async_callback(pool_base.close)
                app.state.pool = pool_base
            if dlq is not None and getattr(app.state, "pool", None) is not None:
                app.state.dlq = dlq
                app.state.cola_descarte = armar_cola(app.state.pool, dlq, reloj_app, settings)
            else:
                _cola_descarte(app, settings, reloj_app)
            _resumen(app, reloj_app)
            # LIFO al cerrar: los consumidores paran primero y el relay publica lo último que
            # escribieron (R-77).
            await pila.enter_async_context(_relay(app, settings, reloj_app))
            await pila.enter_async_context(_consumidores(app, settings, reloj_app, siniestros))
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    app.include_router(descartes.router)
    app.include_router(resumen.router)

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
