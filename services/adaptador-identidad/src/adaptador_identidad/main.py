from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from solventa_common import correlation
from solventa_common.errors import registrar_manejadores
from solventa_common.resiliencia import SinProteccion
from solventa_common.telemetry import instrumentar

from adaptador_identidad.adapters.inbound.http import verificaciones
from adaptador_identidad.adapters.outbound.kyc_http import KycHttp
from adaptador_identidad.application.verificaciones.verificar_identidad import VerificarIdentidad
from adaptador_identidad.config import Settings


def crear_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] = (),
) -> FastAPI:
    """App del adaptador. Las pruebas inyectan el transporte HTTP y los exportadores."""
    settings = settings or Settings()

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as pila:
            # Propaga X-Correlation-Id y traceparent.
            cliente_kyc = await pila.enter_async_context(
                correlation.cliente_async(transport=transport)
            )
            app.state.verificar_identidad = VerificarIdentidad(
                proveedor=KycHttp(
                    cliente_kyc, settings.ally_kyc_url, settings.ally_kyc_credentials.client_secret
                ),
                # Solo timeout; la protección completa es de la historia de consulta en paralelo
                # (W12). El corte es el timeout del proveedor (3 s en infra): una verificación
                # biométrica no cabe en los 700 ms de las consultas de perfilamiento.
                proteccion=SinProteccion(corte_duro_ms=settings.ally_kyc_timeout_ms),
                timeout_ms=settings.ally_kyc_timeout_ms,
            )
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    app.include_router(verificaciones.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = crear_app()
