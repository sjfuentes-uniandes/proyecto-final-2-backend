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
from solventa_common.cache import crear_cache
from solventa_common.errors import registrar_manejadores
from solventa_common.telemetry import instrumentar

from cotizacion.adapters.inbound.http import cotizaciones, perfilamiento
from cotizacion.adapters.outbound.adaptador_datos_http import AdaptadorDatosHttp
from cotizacion.adapters.outbound.catalogo_http import CatalogoHttp
from cotizacion.adapters.outbound.clientes_http import ClientesHttp
from cotizacion.adapters.outbound.productos_catalogo import ProductosCatalogo
from cotizacion.adapters.outbound.reloj import RelojSistema
from cotizacion.application.cotizacion.calcular_prima import CalcularPrima, ConfigCotizacion
from cotizacion.application.cotizacion.consultar import ConsultarCondiciones, ConsultarCotizacion
from cotizacion.application.cotizacion.puertos import PuertoProductos, RepositorioCotizaciones
from cotizacion.application.perfilamiento.crear_perfil import ConfigPerfilamiento, CrearPerfil
from cotizacion.application.perfilamiento.ejecutor_fuentes import EjecutorParalelo
from cotizacion.application.perfilamiento.puertos import (
    EjecutorFuentes,
    PuertoConsentimiento,
    PuertoIdentidad,
    PuertoModeloRiesgo,
    Reloj,
    RepositorioPerfiles,
)
from cotizacion.application.perfilamiento.recalcular_perfil import RecalcularPerfil
from cotizacion.application.perfilamiento.reconstruir_perfil import ReconstruirPerfil
from cotizacion.config import Settings
from cotizacion.domain.perfilamiento.plan_fuentes import plan_fuentes


@dataclass(frozen=True, slots=True)
class Puertos:
    """Implementaciones de los puertos; las pruebas de contrato inyectan fakes."""

    identidad: PuertoIdentidad
    consentimiento: PuertoConsentimiento
    modelos: PuertoModeloRiesgo
    ejecutor: EjecutorFuentes
    perfiles: RepositorioPerfiles
    reloj: Reloj
    # HU-W03. Opcionales para no romper a quien construye Puertos sin ellos (pruebas de W10–W31):
    # sin ambos, las rutas de la cotización responden 503.
    productos: PuertoProductos | None = None
    cotizaciones: RepositorioCotizaciones | None = None


def _configurar_casos(app: FastAPI, puertos: Puertos, settings: Settings) -> None:
    config = ConfigPerfilamiento(
        dependencia_timeout_ms=settings.dependencia_timeout_ms,
        fuente_timeout_ms=settings.fuente_timeout_ms,
        presupuesto_ms=settings.perfilamiento_presupuesto_ms,
    )
    app.state.crear_perfil = CrearPerfil(
        identidad=puertos.identidad,
        consentimiento=puertos.consentimiento,
        modelos=puertos.modelos,
        plan_fuentes=plan_fuentes,
        ejecutor=puertos.ejecutor,
        repo=puertos.perfiles,
        reloj=puertos.reloj,
        config=config,
    )
    app.state.reconstruir_perfil = ReconstruirPerfil(puertos.perfiles, puertos.modelos)
    app.state.recalcular_perfil = RecalcularPerfil(app.state.crear_perfil, puertos.perfiles)
    app.state.perfiles = puertos.perfiles
    if puertos.productos is not None and puertos.cotizaciones is not None:
        app.state.calcular_prima = CalcularPrima(
            productos=puertos.productos,
            identidad=puertos.identidad,
            perfiles=puertos.perfiles,
            repo=puertos.cotizaciones,
            reloj=puertos.reloj,
            config=ConfigCotizacion(
                dependencia_timeout_ms=settings.dependencia_timeout_ms,
                presupuesto_ms=settings.cotizacion_presupuesto_ms,
            ),
        )
        app.state.consultar_cotizacion = ConsultarCotizacion(puertos.cotizaciones)
        app.state.consultar_condiciones = ConsultarCondiciones(puertos.productos, puertos.reloj)


def crear_app(
    settings: Settings | None = None,
    *,
    puertos: Puertos | None = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de cotizacion. Las pruebas inyectan los puertos y los exportadores."""
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as pila:
            if puertos is not None:
                _configurar_casos(app, puertos, settings)
            elif settings.db_host or settings.database_url_override:
                from psycopg_pool import AsyncConnectionPool

                from cotizacion.adapters.outbound.cotizaciones_pg import CotizacionesPg
                from cotizacion.adapters.outbound.perfiles_pg import PerfilesPg, migrar

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
                # Un cliente para las tres dependencias; propaga X-Correlation-Id y traceparent.
                cliente = await pila.enter_async_context(
                    correlation.cliente_async(
                        timeout=httpx.Timeout(2.0), limits=httpx.Limits(max_connections=200)
                    )
                )
                reloj = RelojSistema()
                # Solo lectura (diagramas): catalogo actualiza e invalida la caché.
                cache = crear_cache(settings.cache_url, settings.cache_timeout_ms)
                pila.push_async_callback(cache.cerrar)
                clientes = ClientesHttp(
                    cliente, settings.clientes_url, settings.dependencia_timeout_ms
                )
                _configurar_casos(
                    app,
                    Puertos(
                        identidad=clientes,
                        consentimiento=clientes,
                        modelos=CatalogoHttp(
                            cliente,
                            settings.catalogo_url,
                            settings.modelo_cache_ttl_s,
                            settings.dependencia_timeout_ms,
                            reloj,
                        ),
                        ejecutor=EjecutorParalelo(
                            AdaptadorDatosHttp(cliente, settings.adaptador_datos_url, reloj),
                            settings.fuente_timeout_ms,
                        ),
                        perfiles=PerfilesPg(pool),
                        reloj=reloj,
                        productos=ProductosCatalogo(
                            cliente,
                            settings.catalogo_url,
                            settings.dependencia_timeout_ms,
                            reloj,
                            cache=cache if settings.cache_url else None,
                            ttl_s=settings.catalogo_cache_ttl_s,
                        ),
                        cotizaciones=CotizacionesPg(pool),
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
    app.include_router(perfilamiento.router)
    app.include_router(cotizaciones.router)
    app.include_router(cotizaciones.router_productos)

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
