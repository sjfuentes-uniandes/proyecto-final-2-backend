from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import httpx
import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from solventa_common import correlation
from solventa_common.errors import registrar_manejadores
from solventa_common.telemetry import instrumentar

from polizas.adapters.inbound.http import suscripciones
from polizas.adapters.outbound.catalogo_http import CatalogoHttp
from polizas.adapters.outbound.cotizacion_http import CotizacionHttp
from polizas.adapters.outbound.reloj import RelojSistema
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


@dataclass(frozen=True, slots=True)
class Puertos:
    """Implementaciones de los puertos de la suscripción; las pruebas inyectan dobles."""

    insumos: InsumosOfertaPuerto
    reglas: ReglasSuscripcionPuerto
    decisiones: DecisionesRepositorio
    codigos: GeneradorCodigos
    reloj: Reloj


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


def crear_app(
    settings: Settings | None = None,
    *,
    puertos: Puertos | None = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de polizas. Las pruebas inyectan los puertos y los exportadores."""
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as pila:
            if puertos is not None:
                _configurar_casos(app, puertos, settings)
            elif settings.db_host or settings.database_url_override:
                from psycopg_pool import AsyncConnectionPool

                from polizas.adapters.outbound.codigos import CodigosPg
                from polizas.adapters.outbound.decisiones_pg import DecisionesPg, migrar

                migrar(settings.database_url)
                pool = AsyncConnectionPool(
                    settings.database_url,
                    min_size=1,
                    max_size=settings.db_pool_size,
                    open=False,
                )
                await pool.open()
                pila.push_async_callback(pool.close)
                app.state.pool = pool
                # Un cliente para las dos dependencias; propaga X-Correlation-Id y traceparent.
                # Cada llamada fija su propio timeout (DS2-19).
                cliente = await pila.enter_async_context(
                    correlation.cliente_async(
                        timeout=httpx.Timeout(1.0), limits=httpx.Limits(max_connections=100)
                    )
                )
                reloj = RelojSistema()
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
                        decisiones=DecisionesPg(pool),
                        codigos=CodigosPg(pool),
                        reloj=reloj,
                    ),
                    settings,
                )
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    # HU-W13: suscripción automática. HU-W15 y HU-W17 agregarán aceptación y emisión.
    app.include_router(suscripciones.router)

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
