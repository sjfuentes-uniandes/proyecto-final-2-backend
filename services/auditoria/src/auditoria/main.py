import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from solventa_common.inbox import ReceptorControlado, ReceptorSqs, cliente_sqs
from solventa_common.telemetry import instrumentar

from auditoria.adapters.inbound.sqs.lotes import ConsumidorLotes
from auditoria.adapters.outbound.almacen_s3 import AlmacenS3, cliente_s3
from auditoria.adapters.outbound.cadena_pg import CadenaPg, migrar
from auditoria.adapters.outbound.reloj import RelojSistema
from auditoria.application.registrar_lote import Almacen, RegistrarLote, Reloj
from auditoria.config import Settings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# Al cerrar, el consumidor deja de recibir (abandona la espera larga) y termina su lote: una
# escritura en S3 y una transacción corta. ECS da 30 s entre SIGTERM y SIGKILL.
PARADA_CONSUMIDOR_S = 10.0
MENSAJE_CONSUMIDOR_SIN_COLA = "auditoria: sin cola o sin bucket; no se audita"
MENSAJE_CONSUMIDOR_SIN_CONFIGURACION = "auditoria: consumidor sin base; no se audita"
MENSAJE_CONSUMIDOR_NO_ARRANCO = "auditoria: el consumidor no arrancó"
MENSAJE_CONSUMIDOR_DETENIDO = "auditoria: el consumidor se detuvo"
MENSAJE_CONSUMIDOR_SIN_PARAR = "auditoria: el consumidor no se detuvo a tiempo; se cancela"

logger = logging.getLogger("auditoria.ciclo")


def armar_consumidor(
    pool: "AsyncConnectionPool[Any]",
    receptor: ReceptorControlado,
    almacen: Almacen,
    reloj: Reloj,
    lote: int,
) -> ConsumidorLotes:
    """El consumidor de `auditoria`: cadena e inbox sobre el pool, un objeto por lote."""
    return ConsumidorLotes(receptor, RegistrarLote(CadenaPg(pool), almacen, reloj), lote=lote)


@asynccontextmanager
async def _consumidor(app: FastAPI, settings: Settings, reloj: Reloj) -> AsyncIterator[None]:
    """Consume `auditoria` mientras dura el contexto. Activo solo si llegan la cola y el bucket (en
    AWS siempre; en local y pruebas, no). Sin base o sin clientes, lo registra y la app arranca."""
    if not settings.sqs_auditoria_url or not settings.audit_bucket:
        logger.info(
            MENSAJE_CONSUMIDOR_SIN_COLA, extra={"missing": "SQS_AUDITORIA_URL o AUDIT_BUCKET"}
        )
        yield
        return
    pool = getattr(app.state, "pool", None)
    if pool is None:
        logger.error(MENSAJE_CONSUMIDOR_SIN_CONFIGURACION, extra={"missing": "base"})
        yield
        return
    espera = settings.consumidor_espera_larga_s
    try:
        receptor = ReceptorSqs(
            cliente_sqs(
                settings.aws_region, endpoint_url=settings.aws_endpoint_url, espera_larga_s=espera
            ),
            settings.sqs_auditoria_url,
            espera_larga_s=espera,
            max_mensajes=settings.consumidor_lote,
        )
        almacen = AlmacenS3(
            cliente_s3(settings.aws_region, endpoint_url=settings.aws_endpoint_url),
            settings.audit_bucket,
            cifrado=settings.audit_bucket_sse,
        )
    except Exception as error:  # noqa: BLE001 - el mensaje puede traer la configuración
        logger.error(MENSAJE_CONSUMIDOR_NO_ARRANCO, extra={"error_type": type(error).__name__})
        yield
        return
    consumidor = armar_consumidor(pool, receptor, almacen, reloj, settings.consumidor_lote)

    async def correr(parada: asyncio.Event) -> None:
        try:
            await consumidor.ejecutar(parada)
        except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
            logger.error(MENSAJE_CONSUMIDOR_DETENIDO, extra={"error_type": type(error).__name__})

    parada = asyncio.Event()
    tarea = asyncio.create_task(correr(parada), name="consumidor-auditoria")
    app.state.consumidor = tarea
    try:
        yield
    finally:
        parada.set()
        try:
            await asyncio.wait_for(tarea, PARADA_CONSUMIDOR_S)
        except TimeoutError:
            logger.warning(MENSAJE_CONSUMIDOR_SIN_PARAR, extra={"limit_s": PARADA_CONSUMIDOR_S})


def crear_app(
    settings: Settings | None = None,
    *,
    reloj: Reloj | None = None,
    pool: "AsyncConnectionPool[Any] | None" = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de auditoria (worker; solo /health). Las pruebas inyectan el pool y los exportadores."""
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
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
            # Lo último en entrar: al cerrar, primero el consumidor y luego el pool.
            await pila.enter_async_context(_consumidor(app, settings, reloj or RelojSistema()))
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )

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
