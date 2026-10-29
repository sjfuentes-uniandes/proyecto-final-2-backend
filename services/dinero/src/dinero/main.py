import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
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
from dinero.application.recaudo.conciliar import ConciliarRecaudos
from dinero.application.recaudo.puertos import Reloj
from dinero.application.recaudo.recaudar import RecaudarPrima
from dinero.config import Settings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# Al cerrar (HU-W16, 16.11), en dos fases; luego se cierran el cliente del adaptador y el pool. ECS
# da 30 s entre SIGTERM y SIGKILL (stopTimeout, infra/apps/ecs.tf:141); antes del lifespan uvicorn
# termina las solicitudes en curso (solo /health).
# - Fase 1: se señalizan a la vez el consumidor y la conciliación, y se espera a los dos en paralelo,
#   cada uno con su límite; dura lo que el mayor. Si un límite vence, se cancela: el movimiento
#   queda PENDIENTE (seguro por diseño), y el mensaje vuelve o lo toma otra conciliación.
#   - Consumidor, 20 s: la parada se atiende entre ciclos; un ciclo es una espera larga (10 s) más
#     el lote de 2 en secuencia, cada cobro con un corte de 6,5 s. Cabe la espera más un cobro.
#   - Conciliación, 15 s: la parada se atiende entre movimientos; el que está en curso hace una
#     consulta y, si el cobro no existe, un reintento: 2 × 6,5 s.
# - Fase 2: el relay, 5 s, para publicar los eventos del último cobro y de la última conciliación.
#   Un ciclo normal tarda ms; uno cortado hace rollback y se republica (al menos una vez).
# max(20, 15) + 5 = 25 s, con 5 s de margen para uvicorn y los cierres. En serie serían 40 s.
PARADA_CONSUMIDOR_S = 20.0
PARADA_CONCILIACION_S = 15.0
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
MENSAJE_CONCILIACION_SIN_BASE = "conciliacion: sin base; no se concilia"
MENSAJE_CONCILIACION_DETENIDA = "conciliacion: la conciliación se detuvo"
MENSAJE_CONCILIACION_SIN_PARAR = "conciliacion: la conciliación no se detuvo a tiempo; se cancela"

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


def armar_conciliacion(
    pool: "AsyncConnectionPool[Any]",
    pagos: AdaptadorPagosHttp,
    reloj: Reloj,
    settings: Settings,
) -> ConciliarRecaudos[Any]:
    """La conciliación (16.11): movimiento y outbox sobre el mismo pool, como el consumidor."""
    recaudos = RecaudosPg(pool)
    return ConciliarRecaudos(
        recaudos=recaudos,
        transacciones=recaudos,
        eventos=OutboxRecaudosPg(),
        pagos=pagos,
        reloj=reloj,
        intervalo_s=settings.conciliacion_intervalo_s,
        antiguedad_min_s=settings.conciliacion_antiguedad_min_s,
        lote=settings.conciliacion_lote,
        alerta_tras_s=settings.conciliacion_alerta_tras_s,
    )


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
            logger.warning(fondo.sin_parar, extra={"limit_s": fondo.limite_s})

    await asyncio.gather(*(esperar(fondo) for fondo in activos))


def _relay_outbox(app: FastAPI, settings: Settings, reloj: Reloj) -> _Fondo | None:
    """Como polizas (16.6): vacía el outbox hacia el tópico, sobre el pool. Sin `OUTBOX_ENABLED`
    no hace nada; sin tópico, base o cliente SNS, lo registra y la app arranca igual."""
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


@asynccontextmanager
async def _adaptador_pagos(app: FastAPI, settings: Settings) -> AsyncIterator[AdaptadorPagosHttp]:
    """El cliente de adaptador-pagos que comparten el consumidor y la conciliación. Propaga
    X-Correlation-Id y traceparent; cada llamada fija su timeout. Se cierra después de detenerlos."""
    async with correlation.cliente_async(timeout=httpx.Timeout(1.0)) as cliente_pagos:
        pagos = AdaptadorPagosHttp(
            cliente_pagos, settings.adaptador_pagos_url, settings.adaptador_pagos_timeout_ms
        )
        app.state.cliente_pagos = cliente_pagos
        app.state.pagos = pagos
        yield pagos


def _consumidor_ofertas(
    app: FastAPI, settings: Settings, reloj: Reloj, pagos: AdaptadorPagosHttp
) -> _Fondo | None:
    """HU-W16 (16.10): consume `dinero-aceptaciones` (OfertaAceptada) y cobra. Activo solo si llega
    la cola; sin base o sin cliente SQS, lo registra y la app arranca igual."""
    cola = settings.sqs_dinero_aceptaciones_url
    if not cola:
        logger.warning(
            MENSAJE_CONSUMIDOR_SIN_COLA, extra={"missing": "SQS_DINERO_ACEPTACIONES_URL"}
        )
        return None
    pool = getattr(app.state, "pool", None)
    if pool is None:
        logger.error(MENSAJE_CONSUMIDOR_SIN_CONFIGURACION, extra={"missing": "base"})
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
        logger.error(MENSAJE_CONSUMIDOR_NO_ARRANCO, extra={"error_type": type(error).__name__})
        return None
    consumidor = armar_consumidor(pool, receptor, pagos, reloj)
    return _lanzar(
        app,
        "consumidor_ofertas",
        consumidor.ejecutar,
        limite_s=PARADA_CONSUMIDOR_S,
        detenido=MENSAJE_CONSUMIDOR_DETENIDO,
        sin_parar=MENSAJE_CONSUMIDOR_SIN_PARAR,
    )


def _conciliacion(
    app: FastAPI, settings: Settings, reloj: Reloj, pagos: AdaptadorPagosHttp
) -> _Fondo | None:
    """HU-W16 (16.11): concilia los PENDIENTE antiguos. Encendida por defecto; sin base no arranca
    (lo registra) y la app arranca igual. No depende de la cola."""
    if not settings.conciliacion_habilitada:
        return None
    pool = getattr(app.state, "pool", None)
    if pool is None:
        logger.warning(MENSAJE_CONCILIACION_SIN_BASE, extra={"missing": "base"})
        return None
    conciliacion = armar_conciliacion(pool, pagos, reloj, settings)
    return _lanzar(
        app,
        "conciliacion",
        conciliacion.ejecutar,
        limite_s=PARADA_CONCILIACION_S,
        detenido=MENSAJE_CONCILIACION_DETENIDA,
        sin_parar=MENSAJE_CONCILIACION_SIN_PARAR,
    )


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
            # HU-W16: al cerrar, el consumidor y la conciliación a la vez, luego el relay; la pila
            # cierra después el cliente del adaptador y el pool.
            pagos = await pila.enter_async_context(_adaptador_pagos(app, settings))
            relay: _Fondo | None = None
            productores: list[_Fondo | None] = []
            try:
                relay = _relay_outbox(app, settings, reloj)
                productores.append(_consumidor_ofertas(app, settings, reloj, pagos))
                productores.append(_conciliacion(app, settings, reloj, pagos))
                yield
            finally:
                await _detener(*productores)
                await _detener(relay)

    app = instrumentar(FastAPI(title=settings.service_name, lifespan=ciclo), settings)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = crear_app()
