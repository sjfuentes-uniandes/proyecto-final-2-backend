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

from adaptador_firma.adapters.inbound.http import firmas
from adaptador_firma.adapters.outbound.firma_http import FirmaHttp
from adaptador_firma.application.firmas.firmar import Firmar
from adaptador_firma.config import Settings


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
            cliente_firma = await pila.enter_async_context(
                correlation.cliente_async(transport=transport)
            )
            # Solo timeout y sin reintentos: la emisión sigue EMITIENDO y `polizas` reintenta con
            # la misma clave (ADR 0014). El corte es el timeout del proveedor (5 s en infra): una
            # firma no cabe en los 700 ms de las consultas de perfilamiento.
            app.state.firmar = Firmar(
                proveedor=FirmaHttp(
                    cliente_firma,
                    settings.ally_firma_url,
                    settings.ally_firma_credentials.client_secret,
                ),
                proteccion=SinProteccion(corte_duro_ms=settings.ally_firma_timeout_ms),
                timeout_ms=settings.ally_firma_timeout_ms,
            )
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    app.include_router(firmas.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = crear_app()
