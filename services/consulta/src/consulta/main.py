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
from solventa_common.telemetry import instrumentar

from consulta.adapters.inbound.http.polizas import router as polizas_router
from consulta.adapters.inbound.sqs.polizas import manejador_polizas
from consulta.adapters.outbound.catalogo_http import CatalogoHttp
from consulta.adapters.outbound.lectura_pg import LecturaPolizasPg
from consulta.adapters.outbound.polizas_pg import ProyeccionPolizasPg, migrar
from consulta.adapters.outbound.reloj import RelojSistema
from consulta.application.lectura.casos import ConsultarPoliza, ListarPolizas
from consulta.application.proyeccion.proyectar_poliza import ProyectarPoliza
from consulta.application.proyeccion.puertos import ConsultaCatalogo, Reloj
from consulta.config import Settings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# Al cerrar, el consumidor se espera hasta su límite y luego se cancela; después se cierran el pool
# y el cliente HTTP. ECS da 30 s entre SIGTERM y SIGKILL: un ciclo es una espera larga (10 s) más
# el lote (transacciones cortas), con margen para uvicorn y los cierres.
PARADA_CONSUMIDOR_S = 15.0
MENSAJE_CONSUMIDOR_SIN_COLA = "inbox: sin cola de proyección; no se proyecta"
MENSAJE_CONSUMIDOR_SIN_CONFIGURACION = "inbox: consumidor sin configuración; no se proyecta"
MENSAJE_CONSUMIDOR_NO_ARRANCO = "inbox: el consumidor no arrancó"
MENSAJE_CONSUMIDOR_DETENIDO = "inbox: el consumidor se detuvo"
MENSAJE_CONSUMIDOR_SIN_PARAR = "inbox: el consumidor no se detuvo a tiempo; se cancela"

logger_inbox = logging.getLogger("consulta.inbox")

Ejecutar = Callable[[asyncio.Event], Awaitable[None]]


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


def armar_consumidor(
    pool: "AsyncConnectionPool[Any]",
    receptor: Receptor,
    catalogo: ConsultaCatalogo,
    reloj: Reloj,
) -> ConsumidorInbox[Any]:
    """El consumidor de `consulta-proyeccion` (modo a): inbox y proyección sobre el mismo pool, en
    una transacción por mensaje."""
    caso = ProyectarPoliza(catalogo, ProyeccionPolizasPg(), reloj)
    return ConsumidorInbox(receptor, InboxPg(pool), manejador_polizas(caso), reloj=reloj.ahora)


@asynccontextmanager
async def _consumidor_proyeccion(
    app: FastAPI, settings: Settings, catalogo: ConsultaCatalogo, reloj: Reloj
) -> AsyncIterator[None]:
    """Consume `consulta-proyeccion` y proyecta las pólizas, sobre el pool.

    Activo solo si llega la cola (en AWS siempre; en local y pruebas, no). Sin base, o si no se
    puede crear el cliente SQS, lo registra y la app arranca igual.
    """
    cola = settings.sqs_consulta_proyeccion_url
    if not cola:
        logger_inbox.info(
            MENSAJE_CONSUMIDOR_SIN_COLA, extra={"missing": "SQS_CONSULTA_PROYECCION_URL"}
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
    consumidor = armar_consumidor(pool, receptor, catalogo, reloj)
    async with _en_fondo(
        app,
        "consumidor_proyeccion",
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
    catalogo: ConsultaCatalogo | None = None,
    reloj: Reloj | None = None,
    pool: "AsyncConnectionPool[Any] | None" = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de consulta. Las pruebas inyectan el catálogo, el reloj, el pool y los exportadores."""
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as pila:
            reloj_app: Reloj = reloj or RelojSistema()
            catalogo_app = catalogo
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
            lectura_pool = getattr(app.state, "pool", None)
            if lectura_pool is not None:
                lectura = LecturaPolizasPg(lectura_pool)
                app.state.listar_polizas = ListarPolizas(lectura, reloj_app)
                app.state.consultar_poliza = ConsultarPoliza(lectura, reloj_app)
            if catalogo_app is None:
                # Propaga X-Correlation-Id y traceparent; cada llamada fija su propio timeout.
                cliente = await pila.enter_async_context(
                    correlation.cliente_async(timeout=httpx.Timeout(1.0))
                )
                catalogo_app = CatalogoHttp(
                    cliente, settings.catalogo_url, settings.catalogo_timeout_ms
                )
            # Lo último en entrar: al cerrar (en orden inverso), primero el consumidor y luego el
            # pool.
            await pila.enter_async_context(
                _consumidor_proyeccion(app, settings, catalogo_app, reloj_app)
            )
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    app.include_router(polizas_router)

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
