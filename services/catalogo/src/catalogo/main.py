from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from solventa_common.cache import Cache, crear_cache
from solventa_common.errors import registrar_manejadores
from solventa_common.telemetry import instrumentar

from catalogo.adapters.inbound.http import modelos_riesgo, productos, reglas_suscripcion
from catalogo.adapters.outbound.productos_cache import ProductosConCache
from catalogo.adapters.outbound.reloj import RelojSistema
from catalogo.application.productos.consultar_producto import ConsultarProductos
from catalogo.application.productos.puertos import RepositorioProductos
from catalogo.application.reglas.consultar_modelo import ConsultarModelo
from catalogo.application.reglas.publicar_modelo import PublicarModelo
from catalogo.application.reglas.puertos import Reloj, RepositorioModelos
from catalogo.application.suscripcion.consultar_reglas import ConsultarReglasSuscripcion
from catalogo.application.suscripcion.puertos import RepositorioReglasSuscripcion
from catalogo.config import Settings


async def _abrir_base(
    app: FastAPI, pila: AsyncExitStack, settings: Settings, cache: Cache | None, reloj: Reloj
) -> tuple[RepositorioModelos, RepositorioProductos, RepositorioReglasSuscripcion]:
    """Migra, abre el pool y la caché, e invalida los productos de la semilla (HU-W03)."""
    from psycopg_pool import AsyncConnectionPool

    from catalogo.adapters.outbound.modelos_pg import ModelosPg, migrar, sembrar_qa
    from catalogo.adapters.outbound.productos_pg import (
        PRODUCTOS_QA,
        ProductosPg,
        semillas,
    )
    from catalogo.adapters.outbound.productos_pg import (
        sembrar_qa as sembrar_productos_qa,
    )
    from catalogo.adapters.outbound.reglas_suscripcion_pg import ReglasSuscripcionPg

    migrar(settings.database_url)
    if settings.semilla_qa:
        sembrar_qa(settings.database_url, settings.semilla_qa)
    if settings.productos_qa:
        sembrar_productos_qa(settings.database_url)
    pool = AsyncConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=settings.db_pool_size,
        open=False,
    )
    await pool.open()
    pila.push_async_callback(pool.close)
    app.state.pool = pool
    cache_productos = cache or crear_cache(settings.cache_url, settings.cache_timeout_ms)
    pila.push_async_callback(cache_productos.cerrar)
    con_cache = ProductosConCache(ProductosPg(pool), cache_productos, reloj, settings.cache_ttl_s)
    # La semilla pudo cambiar con este despliegue: catalogo invalida la caché (diagramas).
    sembrados = semillas() + (semillas(PRODUCTOS_QA) if settings.productos_qa else [])
    await con_cache.invalidar([p.id for p, _ in sembrados])
    return ModelosPg(pool), con_cache, ReglasSuscripcionPg(pool)


def crear_app(
    settings: Settings | None = None,
    *,
    repositorio: RepositorioModelos | None = None,
    productos_repo: RepositorioProductos | None = None,
    reglas_suscripcion_repo: RepositorioReglasSuscripcion | None = None,
    cache: Cache | None = None,
    reloj: Reloj | None = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de catalogo. Las pruebas inyectan los repositorios, la caché, el reloj y los
    exportadores."""
    settings = settings or Settings()
    reloj = reloj or RelojSistema()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as pila:
            repo, repo_productos = repositorio, productos_repo
            repo_suscripcion = reglas_suscripcion_repo
            # Sin base configurada (pruebas) no se migra ni se abre el pool.
            if repo is None and (settings.db_host or settings.database_url_override):
                repo, repo_productos, repo_suscripcion = await _abrir_base(
                    app, pila, settings, cache, reloj
                )
            if repo is not None:
                app.state.reloj = reloj
                app.state.consultar_modelo = ConsultarModelo(repo)
                app.state.publicar_modelo = PublicarModelo(repo, reloj)
            if repo_productos is not None:
                app.state.consultar_productos = ConsultarProductos(repo_productos, reloj)
                if repo_suscripcion is not None:
                    # HU-W13: el producto inexistente se distingue con el repositorio de productos.
                    app.state.consultar_reglas_suscripcion = ConsultarReglasSuscripcion(
                        repo_suscripcion, repo_productos, reloj
                    )
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    app.include_router(modelos_riesgo.router)
    app.include_router(productos.router)
    app.include_router(reglas_suscripcion.router)

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
