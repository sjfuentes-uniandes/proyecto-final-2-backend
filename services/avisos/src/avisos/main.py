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
from solventa_common.inbox import ConsumidorInbox, InboxPg, Receptor, ReceptorSqs, cliente_sqs
from solventa_common.resiliencia import SinProteccion
from solventa_common.telemetry import instrumentar

from avisos.adapters.inbound.sqs.polizas import manejador_polizas
from avisos.adapters.outbound.entregas_pg import EntregasPg, migrar
from avisos.adapters.outbound.push_http import EnviadorPushHttp
from avisos.adapters.outbound.reloj import RelojSistema
from avisos.application.entregas.barrido import Barrido
from avisos.application.entregas.enviar import EnviarEntregas
from avisos.application.entregas.puertos import EnviadorPush, Reloj
from avisos.application.entregas.registrar import RegistrarEntregas
from avisos.config import Settings
from avisos.domain.avisos.modelos import PoliticaReintento

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# Al cerrar, cada tarea se espera hasta su límite y luego se cancela; después se cierra el pool. ECS
# da 30 s entre SIGTERM y SIGKILL: un ciclo del consumidor es una espera larga (10 s) más el lote.
PARADA_TAREA_S = 15.0
MENSAJE_SIN_COLA = "inbox: sin cola de avisos; no se consume"
MENSAJE_SIN_BASE = "avisos: sin base; no se consume ni se barre"
MENSAJE_CONSUMIDOR_NO_ARRANCO = "inbox: el consumidor no arrancó"
MENSAJE_TAREA_DETENIDA = "avisos: una tarea de fondo se detuvo"
MENSAJE_TAREA_SIN_PARAR = "avisos: una tarea de fondo no se detuvo a tiempo; se cancela"

logger = logging.getLogger("avisos.main")

Ejecutar = Callable[[asyncio.Event], Awaitable[None]]


@asynccontextmanager
async def _en_fondo(app: FastAPI, atributo: str, ejecutar: Ejecutar) -> AsyncIterator[None]:
    """Corre `ejecutar` como tarea (`app.state.<atributo>`) mientras dura el contexto. Al cerrar,
    señaliza la parada y espera a la tarea hasta `PARADA_TAREA_S`; si no termina, se cancela."""

    async def correr(parada: asyncio.Event) -> None:
        try:
            await ejecutar(parada)
        except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
            logger.error(
                MENSAJE_TAREA_DETENIDA,
                extra={"tarea": atributo, "error_type": type(error).__name__},
            )

    parada = asyncio.Event()
    tarea = asyncio.create_task(correr(parada), name=atributo.replace("_", "-"))
    setattr(app.state, atributo, tarea)
    try:
        yield
    finally:
        parada.set()
        try:
            await asyncio.wait_for(tarea, PARADA_TAREA_S)
        except TimeoutError:
            logger.warning(MENSAJE_TAREA_SIN_PARAR, extra={"tarea": atributo})


def armar_envio(
    pool: "AsyncConnectionPool[Any]", enviador: EnviadorPush, reloj: Reloj, settings: Settings
) -> tuple[RegistrarEntregas[Any], EnviarEntregas[Any]]:
    entregas = EntregasPg(pool)
    politica = PoliticaReintento(
        settings.entregas_max_intentos,
        settings.entregas_espera_inicial_s,
        settings.entregas_espera_max_s,
    )
    return (
        RegistrarEntregas(entregas, reloj),
        EnviarEntregas(entregas, enviador, reloj, politica, settings.entregas_lease_s),
    )


def armar_consumidor(
    pool: "AsyncConnectionPool[Any]",
    receptor: Receptor,
    enviador: EnviadorPush,
    reloj: Reloj,
    settings: Settings,
) -> ConsumidorInbox[Any]:
    """El consumidor de la cola `avisos` (modo b): inbox y entregas PENDIENTES en una transacción y,
    tras el commit, el primer intento de envío."""
    registrar, enviar = armar_envio(pool, enviador, reloj, settings)
    return ConsumidorInbox(
        receptor, InboxPg(pool), manejador_polizas(registrar, enviar), reloj=reloj.ahora
    )


@asynccontextmanager
async def _tareas_de_fondo(
    app: FastAPI, settings: Settings, enviador: EnviadorPush, reloj: Reloj
) -> AsyncIterator[None]:
    """El barrido (si hay base) y el consumidor (si además llega la cola)."""
    pool = getattr(app.state, "pool", None)
    if pool is None:
        logger.info(MENSAJE_SIN_BASE)
        yield
        return
    _, enviar = armar_envio(pool, enviador, reloj, settings)
    barrido = Barrido(enviar, settings.entregas_barrido_intervalo_s)
    async with AsyncExitStack() as pila:
        await pila.enter_async_context(_en_fondo(app, "barrido", barrido.ejecutar))
        cola = settings.sqs_avisos_url
        if not cola:
            logger.error(MENSAJE_SIN_COLA, extra={"missing": "SQS_AVISOS_URL"})
        else:
            espera = settings.consumidor_espera_larga_s
            try:
                receptor = ReceptorSqs(
                    cliente_sqs(
                        settings.aws_region,
                        endpoint_url=settings.aws_endpoint_url,
                        espera_larga_s=espera,
                    ),
                    cola,
                    espera_larga_s=espera,
                    max_mensajes=settings.consumidor_max_mensajes,
                )
            except Exception as error:  # noqa: BLE001 - el mensaje puede traer la configuración
                logger.error(
                    MENSAJE_CONSUMIDOR_NO_ARRANCO, extra={"error_type": type(error).__name__}
                )
            else:
                consumidor = armar_consumidor(pool, receptor, enviador, reloj, settings)
                await pila.enter_async_context(
                    _en_fondo(app, "consumidor_avisos", consumidor.ejecutar)
                )
        yield


def crear_app(
    settings: Settings | None = None,
    *,
    enviador: EnviadorPush | None = None,
    reloj: Reloj | None = None,
    pool: "AsyncConnectionPool[Any] | None" = None,
    transport: httpx.AsyncBaseTransport | None = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de avisos. Las pruebas inyectan el enviador, el reloj, el pool, el transporte HTTP del
    envío push y los exportadores."""
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as pila:
            reloj_app: Reloj = reloj or RelojSistema()
            if enviador is not None:
                enviador_app: EnviadorPush = enviador
            else:
                # Propaga X-Correlation-Id y traceparent hacia adaptador-firma.
                cliente_firma = await pila.enter_async_context(
                    correlation.cliente_async(transport=transport)
                )
                # Sin ADAPTADOR_FIRMA_URL (local y pruebas sin inyectar), falla cerrado: el envío
                # nunca marca ENVIADO sin haber llamado de verdad al proveedor.
                enviador_app = EnviadorPushHttp(
                    cliente_firma,
                    settings.adaptador_firma_url,
                    settings.adaptador_firma_timeout_ms,
                    proteccion=SinProteccion(corte_duro_ms=settings.adaptador_firma_timeout_ms),
                )
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
            # Lo último en entrar: al cerrar (en orden inverso), primero las tareas y luego el pool.
            await pila.enter_async_context(_tareas_de_fondo(app, settings, enviador_app, reloj_app))
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)

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
