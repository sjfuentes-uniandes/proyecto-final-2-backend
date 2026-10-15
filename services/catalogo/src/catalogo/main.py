from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from solventa_common.errors import registrar_manejadores
from solventa_common.telemetry import instrumentar

from catalogo.adapters.inbound.http import modelos_riesgo
from catalogo.adapters.outbound.reloj import RelojSistema
from catalogo.application.reglas.consultar_modelo import ConsultarModelo
from catalogo.application.reglas.publicar_modelo import PublicarModelo
from catalogo.application.reglas.puertos import Reloj, RepositorioModelos
from catalogo.config import Settings


def crear_app(
    settings: Settings | None = None,
    *,
    repositorio: RepositorioModelos | None = None,
    reloj: Reloj | None = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de catalogo. Las pruebas inyectan el repositorio, el reloj y los exportadores."""
    settings = settings or Settings()
    reloj = reloj or RelojSistema()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as pila:
            repo = repositorio
            # Sin base configurada (pruebas) no se migra ni se abre el pool.
            if repo is None and (settings.db_host or settings.database_url_override):
                from psycopg_pool import AsyncConnectionPool

                from catalogo.adapters.outbound.modelos_pg import ModelosPg, migrar

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
                repo = ModelosPg(pool)
            if repo is not None:
                app.state.reloj = reloj
                app.state.consultar_modelo = ConsultarModelo(repo)
                app.state.publicar_modelo = PublicarModelo(repo, reloj)
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    app.include_router(modelos_riesgo.router)

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
