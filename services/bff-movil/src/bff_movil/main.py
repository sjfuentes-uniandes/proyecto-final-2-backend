from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from solventa_common import correlation
from solventa_common.errors import registrar_manejadores
from solventa_common.telemetry import instrumentar

from bff_movil.adapters.inbound.http import consentimientos, registro, sesion, verificacion
from bff_movil.adapters.inbound.http.auth import Autenticador
from bff_movil.adapters.inbound.http.jwks import ProveedorJwks
from bff_movil.adapters.outbound.clientes_http import ClientesHttp
from bff_movil.adapters.outbound.consentimientos_http import ConsentimientosHttp
from bff_movil.adapters.outbound.registro_http import RegistroHttp
from bff_movil.adapters.outbound.resolutor_cliente_http import ResolutorClienteHttp
from bff_movil.adapters.outbound.verificacion_http import VerificacionHttp
from bff_movil.application.consentimientos.casos import (
    ConsultarConsentimientos,
    ConsultarTextoVigente,
    OtorgarConsentimiento,
)
from bff_movil.application.registro.casos import VerificarRegistro, VincularRegistro
from bff_movil.application.sesion.consultar_sesion import ConsultarSesion
from bff_movil.application.sesion.puertos import ResolutorCliente
from bff_movil.application.verificacion.verificar_identidad import VerificarIdentidad
from bff_movil.config import Settings


def crear_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolutor: ResolutorCliente | None = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App del BFF móvil. Las pruebas inyectan el transporte, el resolutor y los exportadores."""
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as pila:
            # Un AsyncClient por dependencia, creado una vez; el de clientes propaga
            # X-Correlation-Id y traceparent.
            cliente_clientes = await pila.enter_async_context(
                correlation.cliente_async(transport=transport)
            )
            cliente_jwks = await pila.enter_async_context(httpx.AsyncClient(transport=transport))
            app.state.autenticador = Autenticador(
                settings,
                ProveedorJwks(
                    cliente_jwks,
                    ttl_s=settings.jwks_ttl_s,
                    timeout_ms=settings.jwks_timeout_ms,
                    refresco_min_s=settings.jwks_refresco_min_s,
                ),
            )
            app.state.consultar_sesion = ConsultarSesion(
                resolutor
                or ResolutorClienteHttp(
                    cliente_clientes, settings.clientes_url, settings.clientes_timeout_ms
                ),
                ClientesHttp(cliente_clientes, settings.clientes_url, settings.clientes_timeout_ms),
            )
            app.state.verificar_identidad = VerificarIdentidad(
                VerificacionHttp(
                    cliente_clientes, settings.clientes_url, settings.verificacion_timeout_ms
                )
            )
            servicio_registro = RegistroHttp(
                cliente_clientes,
                settings.clientes_url,
                settings.verificacion_timeout_ms,
                settings.vinculacion_timeout_ms,
            )
            app.state.verificar_registro = VerificarRegistro(servicio_registro)
            app.state.vincular_registro = VincularRegistro(servicio_registro)
            app.state.secreto_registro = settings.registro_token_secret.get_secret_value()
            servicio_consentimientos = ConsentimientosHttp(
                cliente_clientes,
                settings.clientes_url,
                settings.clientes_timeout_ms,
                settings.otorgamiento_timeout_ms,
            )
            app.state.otorgar_consentimiento = OtorgarConsentimiento(servicio_consentimientos)
            app.state.consultar_consentimientos = ConsultarConsentimientos(servicio_consentimientos)
            app.state.consultar_texto_vigente = ConsultarTextoVigente(servicio_consentimientos)
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    app.include_router(sesion.router)
    app.include_router(verificacion.router)
    app.include_router(registro.router)
    app.include_router(consentimientos.router)
    if settings.environment == "local":
        # Simula el Lambda pre sign-up donde no hay Cognito; nunca existe fuera de local.
        app.include_router(registro.router_local)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = crear_app()
