import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from solventa_common.errors import registrar_manejadores
from solventa_common.outbox import OutboxPg, PublicadorSns, RelayOutbox, cliente_sns
from solventa_common.telemetry import instrumentar

from adaptador_telemetria.adapters.inbound.http import eventos
from adaptador_telemetria.adapters.outbound.archivo_s3 import (
    MENSAJE_SIN_BUCKET,
    ArchivoS3,
    ArchivoSinConfigurar,
    cliente_s3,
)
from adaptador_telemetria.adapters.outbound.base_de_datos import migrar
from adaptador_telemetria.adapters.outbound.recibidos_pg import (
    RegistroPg,
    RegistroSinConfigurar,
)
from adaptador_telemetria.adapters.outbound.reloj import RelojSistema
from adaptador_telemetria.application.telemetria.puertos import Archivo, Registro, Reloj
from adaptador_telemetria.application.telemetria.recibir_evento import RecibirEvento
from adaptador_telemetria.config import Settings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# Al cerrar, el relay tiene su límite para publicar lo último (un ciclo normal tarda ms; uno cortado
# hace rollback y se republica). ECS da 30 s entre SIGTERM y SIGKILL.
PARADA_RELAY_S = 5.0
MENSAJE_SIN_SECRETO = "credenciales: sin hmac_secret válido; se rechaza todo envío"
MENSAJE_SIN_BASE = "base: sin configurar; no se acepta ningún evento"
MENSAJE_ARCHIVO_NO_ARRANCO = "archivo: no se pudo crear el cliente S3"
MENSAJE_RELAY_SIN_CONFIGURACION = "outbox: relay habilitado sin configuración; no se publica"
MENSAJE_RELAY_NO_ARRANCO = "outbox: el relay no arrancó"
MENSAJE_RELAY_DETENIDO = "outbox: el relay se detuvo"
MENSAJE_RELAY_SIN_PARAR = "outbox: el relay no se detuvo a tiempo; se cancela"

logger = logging.getLogger("adaptador_telemetria.ciclo")

Ejecutar = Callable[[asyncio.Event], Awaitable[None]]


@asynccontextmanager
async def _en_fondo(app: FastAPI, atributo: str, ejecutar: Ejecutar) -> AsyncIterator[None]:
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
            await asyncio.wait_for(tarea, PARADA_RELAY_S)
        except TimeoutError:
            logger.warning(MENSAJE_RELAY_SIN_PARAR, extra={"limit_s": PARADA_RELAY_S})


def _archivo(settings: Settings) -> Archivo:
    """El original va al bucket de evidencias; sin bucket, no se acepta ningún evento."""
    if not settings.evidencias_bucket:
        logger.error(MENSAJE_SIN_BUCKET)
        return ArchivoSinConfigurar()
    try:
        cliente = cliente_s3(settings.aws_region, settings.s3_timeout_ms, settings.aws_endpoint_url)
    except Exception as error:  # noqa: BLE001 - el mensaje puede traer la configuración
        logger.error(MENSAJE_ARCHIVO_NO_ARRANCO, extra={"error_type": type(error).__name__})
        return ArchivoSinConfigurar()
    return ArchivoS3(
        cliente, settings.evidencias_bucket, settings.evidencias_bucket_sse, settings.s3_timeout_ms
    )


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
    archivo: Archivo | None = None,
    registro: Registro | None = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de adaptador-telemetria. Las pruebas inyectan el pool, el registro, el archivo y los
    exportadores."""
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
                    max_size=settings.db_pool_size,
                    open=False,
                )
                await pool_base.open()
                pila.push_async_callback(pool_base.close)
                app.state.pool = pool_base
            registro_app: Registro
            if registro is not None:
                registro_app = registro
            elif getattr(app.state, "pool", None) is not None:
                registro_app = RegistroPg(app.state.pool)
            else:
                logger.error(MENSAJE_SIN_BASE)
                registro_app = RegistroSinConfigurar()
            secreto = settings.hmac_secret
            if secreto is None:
                logger.error(MENSAJE_SIN_SECRETO)
            app.state.recibir_evento = RecibirEvento(
                secreto=secreto,
                fuentes=settings.telemetria_fuentes,
                desfase_max_s=settings.desfase_max_s,
                prefijo_archivo=settings.telemetria_archivo_prefijo,
                archivo=archivo or _archivo(settings),
                registro=registro_app,
                reloj=reloj_app,
            )
            await pila.enter_async_context(_relay(app, settings, reloj_app))
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(eventos.router)
    eventos.publicar_esquemas(app)
    return app


app = crear_app()
