import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI
from solventa_common import correlation
from solventa_common.inbox import ConsumidorInbox, InboxPg, Receptor, ReceptorSqs, cliente_sqs
from solventa_common.outbox import OutboxPg, PublicadorSns, RelayOutbox, cliente_sns
from solventa_common.telemetry import instrumentar

from dinero.adapters.inbound.sqs.oferta_aceptada import manejador_oferta_aceptada
from dinero.adapters.outbound.outbox_pg import OutboxRecaudosPg
from dinero.adapters.outbound.pagos_http import AdaptadorPagosHttp
from dinero.adapters.outbound.recaudos_pg import RecaudosPg
from dinero.adapters.outbound.reloj import RelojSistema
from dinero.application.recaudo.puertos import Reloj
from dinero.application.recaudo.recaudar import RecaudarPrima
from dinero.config import Settings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# Al cerrar se detiene primero el consumidor (su último cobro escribe un evento en el outbox), luego
# el relay y al final se cierra el pool. ECS da 30 s entre SIGTERM y SIGKILL (stopTimeout,
# infra/apps/ecs.tf:141); antes del lifespan uvicorn termina las solicitudes en curso (solo /health).
# - Consumidor, 20 s: la parada se atiende entre ciclos; un ciclo es una espera larga (10 s) más el
#   lote de 2 en secuencia, cada cobro con un corte de 6,5 s. Cabe la espera más un cobro lento; si
#   vence, se cancela: el movimiento queda PENDIENTE (seguro por diseño) y el mensaje vuelve.
# - Relay, 5 s: un ciclo normal tarda ms; uno cortado hace rollback y se republica (al menos una vez).
# 20 + 5 = 25 s, con 5 s de margen para uvicorn y el cierre del pool.
PARADA_CONSUMIDOR_S = 20.0
PARADA_RELAY_S = 5.0

MENSAJE_RELAY_SIN_CONFIGURACION = "outbox: relay habilitado sin configuración; no se publica"
MENSAJE_RELAY_NO_ARRANCO = "outbox: el relay no arrancó"
MENSAJE_RELAY_DETENIDO = "outbox: el relay se detuvo"
MENSAJE_RELAY_SIN_PARAR = "outbox: el relay no se detuvo a tiempo; se cancela"
MENSAJE_CONSUMIDOR_SIN_COLA = "inbox: sin cola de OfertaAceptada; no se recauda"
MENSAJE_CONSUMIDOR_SIN_CONFIGURACION = "inbox: consumidor sin configuración; no se recauda"
MENSAJE_CONSUMIDOR_NO_ARRANCO = "inbox: el consumidor no arrancó"
MENSAJE_CONSUMIDOR_DETENIDO = "inbox: el consumidor se detuvo"
MENSAJE_CONSUMIDOR_SIN_PARAR = "inbox: el consumidor no se detuvo a tiempo; se cancela"

logger = logging.getLogger("dinero.ciclo")

Ejecutar = Callable[[asyncio.Event], Awaitable[None]]


def armar_consumidor(
    pool: "AsyncConnectionPool[Any]",
    receptor: Receptor,
    pagos: AdaptadorPagosHttp,
    reloj: Reloj,
) -> ConsumidorInbox[Any]:
    """El consumidor de `dinero-aceptaciones`: inbox, movimiento y outbox sobre el mismo pool."""
    recaudos = RecaudosPg(pool)
    caso = RecaudarPrima(
        recaudos=recaudos,
        transacciones=recaudos,
        eventos=OutboxRecaudosPg(),
        pagos=pagos,
        reloj=reloj,
    )
    return ConsumidorInbox(
        receptor, InboxPg(pool), manejador_oferta_aceptada(caso), reloj=reloj.ahora
    )


@asynccontextmanager
async def _en_segundo_plano(
    app: FastAPI,
    atributo: str,
    ejecutar: Ejecutar,
    *,
    limite_s: float,
    detenido: str,
    sin_parar: str,
) -> AsyncIterator[None]:
    """Corre `ejecutar` como tarea mientras la app vive. Un error inesperado se registra y la app
    sigue sirviendo. Al cerrar, señaliza la parada y espera hasta `limite_s`; luego cancela."""

    async def correr(parada: asyncio.Event) -> None:
        try:
            await ejecutar(parada)
        except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
            logger.error(detenido, extra={"error_type": type(error).__name__})

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
            logger.warning(sin_parar, extra={"limit_s": limite_s})


@asynccontextmanager
async def _relay_outbox(app: FastAPI, settings: Settings, reloj: Reloj) -> AsyncIterator[None]:
    """Como polizas (16.6): vacía el outbox hacia el tópico, sobre el pool. Sin `OUTBOX_ENABLED`
    no hace nada; sin tópico, base o cliente SNS, lo registra y la app arranca igual."""
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
    async with _en_segundo_plano(
        app,
        "relay_outbox",
        relay.ejecutar,
        limite_s=PARADA_RELAY_S,
        detenido=MENSAJE_RELAY_DETENIDO,
        sin_parar=MENSAJE_RELAY_SIN_PARAR,
    ):
        yield


@asynccontextmanager
async def _consumidor_ofertas(
    app: FastAPI, settings: Settings, reloj: Reloj
) -> AsyncIterator[None]:
    """HU-W16 (16.10): consume `dinero-aceptaciones` (OfertaAceptada) y cobra. Activo solo si llega
    la cola; sin base o sin cliente SQS, lo registra y la app arranca igual."""
    cola = settings.sqs_dinero_aceptaciones_url
    if not cola:
        logger.warning(
            MENSAJE_CONSUMIDOR_SIN_COLA, extra={"missing": "SQS_DINERO_ACEPTACIONES_URL"}
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
            cola,
            espera_larga_s=espera,
            max_mensajes=settings.consumidor_max_mensajes,
        )
    except Exception as error:  # noqa: BLE001 - el mensaje puede traer la configuración
        logger.error(MENSAJE_CONSUMIDOR_NO_ARRANCO, extra={"error_type": type(error).__name__})
        yield
        return
    # Propaga X-Correlation-Id y traceparent; cada cobro fija su timeout. Se cierra después de
    # detener el consumidor.
    async with correlation.cliente_async(timeout=httpx.Timeout(1.0)) as cliente_pagos:
        pagos = AdaptadorPagosHttp(
            cliente_pagos, settings.adaptador_pagos_url, settings.adaptador_pagos_timeout_ms
        )
        app.state.cliente_pagos = cliente_pagos
        app.state.pagos = pagos
        consumidor = armar_consumidor(pool, receptor, pagos, reloj)
        async with _en_segundo_plano(
            app,
            "consumidor_ofertas",
            consumidor.ejecutar,
            limite_s=PARADA_CONSUMIDOR_S,
            detenido=MENSAJE_CONSUMIDOR_DETENIDO,
            sin_parar=MENSAJE_CONSUMIDOR_SIN_PARAR,
        ):
            yield


def crear_app(
    settings: Settings | None = None, *, pool: "AsyncConnectionPool[Any] | None" = None
) -> FastAPI:
    """App de dinero. Las pruebas inyectan el pool sobre una base ya migrada; lo abren y lo cierran
    ellas."""
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        reloj = RelojSistema()
        async with AsyncExitStack() as pila:
            if pool is not None:
                app.state.pool = pool
            elif settings.db_host or settings.database_url_override:
                from psycopg_pool import AsyncConnectionPool

                from dinero.adapters.outbound.recaudos_pg import migrar

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
            # HU-W16 (16.10): al cerrar, en orden inverso: el consumidor, el relay y el pool.
            await pila.enter_async_context(_relay_outbox(app, settings, reloj))
            await pila.enter_async_context(_consumidor_ofertas(app, settings, reloj))
            yield

    app = instrumentar(FastAPI(title=settings.service_name, lifespan=ciclo), settings)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = crear_app()
