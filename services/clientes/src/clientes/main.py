from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from solventa_common import correlation
from solventa_common.errors import registrar_manejadores
from solventa_common.telemetry import instrumentar

from clientes.adapters.inbound.http import consentimientos, identidad, registro
from clientes.adapters.outbound.adaptador_identidad_http import AdaptadorIdentidadHttp
from clientes.adapters.outbound.textos_json import TextosJson
from clientes.application.consentimientos.casos import (
    ConsultarConsentimientos,
    ConsultarTextoVigente,
    OtorgarConsentimiento,
    RevocarConsentimiento,
)
from clientes.application.consentimientos.puertos import RepositorioConsentimientos
from clientes.application.identidad.casos import (
    ConsultarCliente,
    VerificarIdentidad,
    VerificarRegistro,
    VincularRegistro,
)
from clientes.application.identidad.puertos import RepositorioClientes
from clientes.config import Settings


def crear_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    repositorio: RepositorioClientes | None = None,
    repositorio_consentimientos: RepositorioConsentimientos | None = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de `clientes`. Las pruebas inyectan el transporte, los repositorios y los exportadores.

    Sin `repositorio` se cablea Postgres: se migra y se abre el `AsyncConnectionPool` de los
    consentimientos, que se cierra al apagar. Con `repositorio` inyectado, las rutas de
    consentimiento solo funcionan si también se inyecta `repositorio_consentimientos`.
    """
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        repo = repositorio
        async with AsyncExitStack() as pila:
            repo_consentimientos = repositorio_consentimientos
            if repo is None:
                from psycopg_pool import AsyncConnectionPool

                from clientes.adapters.outbound.consentimientos_pg import ConsentimientosPg
                from clientes.adapters.outbound.postgres import RepositorioPostgres, migrar

                migrar(settings.database_url)
                repo = RepositorioPostgres(settings.database_url)
                if repo_consentimientos is None:
                    pool = AsyncConnectionPool(
                        settings.database_url,
                        min_size=settings.db_pool_min_size,
                        max_size=settings.db_pool_size,
                        open=False,
                    )
                    await pool.open()
                    pila.push_async_callback(pool.close)
                    repo_consentimientos = ConsentimientosPg(pool)
            # Propaga X-Correlation-Id y traceparent hacia adaptador-identidad.
            cliente_http = await pila.enter_async_context(
                correlation.cliente_async(transport=transport)
            )
            verificador = VerificarIdentidad(
                repo,
                AdaptadorIdentidadHttp(
                    cliente_http,
                    settings.adaptador_identidad_url,
                    settings.adaptador_identidad_timeout_ms,
                ),
            )
            app.state.verificar_identidad = verificador
            app.state.verificar_registro = VerificarRegistro(
                repo, verificador, settings.secreto_registro
            )
            app.state.vincular_registro = VincularRegistro(
                repo, settings.secreto_registro, settings.registro_vinculacion_gracia_s
            )
            app.state.consultar_cliente = ConsultarCliente(repo)
            if repo_consentimientos is not None:
                textos = TextosJson.cargar()
                app.state.otorgar_consentimiento = OtorgarConsentimiento(
                    repo_consentimientos, app.state.consultar_cliente, textos
                )
                app.state.consultar_consentimientos = ConsultarConsentimientos(
                    repo_consentimientos, app.state.consultar_cliente
                )
                app.state.revocar_consentimiento = RevocarConsentimiento(
                    repo_consentimientos, app.state.consultar_cliente
                )
                app.state.consultar_texto_vigente = ConsultarTextoVigente(textos)
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    app.include_router(identidad.router)
    app.include_router(registro.router)
    app.include_router(consentimientos.router)
    app.include_router(consentimientos.router_textos)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = crear_app()
