from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from solventa_common import correlation
from solventa_common.errors import registrar_manejadores
from solventa_common.telemetry import instrumentar

from clientes.adapters.inbound.http import (
    alta,
    consentimientos,
    identidad,
    registro,
    verificaciones_consentimiento,
)
from clientes.adapters.outbound.adaptador_identidad_http import AdaptadorIdentidadHttp
from clientes.adapters.outbound.textos_json import TextosJson
from clientes.application.consentimientos.casos import (
    ConsultarConsentimientos,
    ConsultarTextoVigente,
    OtorgarConsentimiento,
    RevocarConsentimiento,
    VerificarConsentimiento,
)
from clientes.application.consentimientos.puertos import (
    RepositorioConsentimientos,
    RepositorioVerificaciones,
)
from clientes.application.identidad.casos import (
    ConsultarCliente,
    VerificarIdentidad,
    VerificarRegistro,
    VincularRegistro,
)
from clientes.application.identidad.documental import CrearCliente, VerificarDocumento
from clientes.application.identidad.puertos import RepositorioClientes
from clientes.config import Settings


def crear_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    repositorio: RepositorioClientes | None = None,
    repositorio_consentimientos: RepositorioConsentimientos | None = None,
    repositorio_verificaciones: RepositorioVerificaciones | None = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App de `clientes`. Las pruebas inyectan el transporte, los repositorios y los exportadores.

    Sin `repositorio` se cablea Postgres: se migra, se siembran C01–C27 si
    `CLIENTES_SEMILLA_SINTETICA` es true y se abre el `AsyncConnectionPool` de los
    consentimientos, que se cierra al apagar; el mismo repositorio atiende la verificación. Con
    `repositorio` inyectado, las rutas de consentimiento solo funcionan si también se inyecta
    `repositorio_consentimientos`, y la de verificación, `repositorio_verificaciones`.
    """
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        repo = repositorio
        async with AsyncExitStack() as pila:
            repo_consentimientos = repositorio_consentimientos
            repo_verificaciones = repositorio_verificaciones
            if repo is None:
                from psycopg_pool import AsyncConnectionPool

                from clientes.adapters.outbound.consentimientos_pg import ConsentimientosPg
                from clientes.adapters.outbound.postgres import (
                    RepositorioPostgres,
                    crear_pool,
                    migrar,
                )

                migrar(settings.database_url)
                if settings.semilla_sintetica:
                    from clientes.adapters.outbound.semilla_sintetica import sembrar

                    sembrar(settings.database_url)
                # Identidad: conexiones reutilizadas (antes, una conexión TLS nueva por lectura).
                pool_identidad = crear_pool(
                    settings.database_url, settings.db_pool_min_size, settings.db_pool_size
                )
                pool_identidad.open()
                pila.callback(pool_identidad.close)
                repo = RepositorioPostgres(settings.database_url, pool_identidad)
                if repo_consentimientos is None:
                    pool = AsyncConnectionPool(
                        settings.database_url,
                        min_size=settings.db_pool_min_size,
                        max_size=settings.db_pool_size,
                        open=False,
                    )
                    await pool.open()
                    pila.push_async_callback(pool.close)
                    pg = ConsentimientosPg(pool)
                    repo_consentimientos = pg
                    if repo_verificaciones is None:
                        repo_verificaciones = pg
            # Propaga X-Correlation-Id y traceparent hacia adaptador-identidad.
            cliente_http = await pila.enter_async_context(
                correlation.cliente_async(transport=transport)
            )
            adaptador_identidad = AdaptadorIdentidadHttp(
                cliente_http,
                settings.adaptador_identidad_url,
                settings.adaptador_identidad_timeout_ms,
            )
            verificador = VerificarIdentidad(repo, adaptador_identidad)
            app.state.verificar_identidad = verificador
            app.state.verificar_registro = VerificarRegistro(
                repo, verificador, settings.secreto_registro
            )
            app.state.vincular_registro = VincularRegistro(
                repo, settings.secreto_registro, settings.registro_vinculacion_gracia_s
            )
            app.state.consultar_cliente = ConsultarCliente(repo)
            app.state.consulta_timeout_ms = settings.consulta_timeout_ms
            app.state.crear_cliente = CrearCliente(repo)
            app.state.verificar_documento = VerificarDocumento(repo, adaptador_identidad)
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
            if repo_verificaciones is not None:
                app.state.verificar_consentimiento = VerificarConsentimiento(repo_verificaciones)
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    app.include_router(identidad.router)
    app.include_router(alta.router)
    app.include_router(registro.router)
    app.include_router(consentimientos.router)
    app.include_router(consentimientos.router_textos)
    app.include_router(verificaciones_consentimiento.router)
    # Después de `consentimientos.router`: `/v1/clientes/actual/...` no debe caer en `{clienteId}`.
    app.include_router(consentimientos.router_web)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = crear_app()
