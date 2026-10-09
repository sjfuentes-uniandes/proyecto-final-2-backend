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

from adaptador_datos.adapters.inbound.http import consultas
from adaptador_datos.adapters.outbound.datos_abiertos_http import DatosAbiertosHttp
from adaptador_datos.adapters.outbound.open_finance_http import OpenFinanceHttp
from adaptador_datos.application.consultas.consultar_fuente import ConsultarFuente
from adaptador_datos.config import Settings
from adaptador_datos.domain.consultas.modelos import Aliado


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
            # Un AsyncClient por aliado; propaga X-Correlation-Id y traceparent.
            open_finance = await pila.enter_async_context(
                correlation.cliente_async(transport=transport)
            )
            datos_abiertos = await pila.enter_async_context(
                correlation.cliente_async(transport=transport)
            )
            app.state.consultar_fuente = ConsultarFuente(
                proveedores={
                    Aliado.OPEN_FINANCE: OpenFinanceHttp(
                        open_finance,
                        settings.ally_open_finance_url,
                        settings.ally_open_finance_credentials.client_secret,
                    ),
                    Aliado.DATOS_ABIERTOS: DatosAbiertosHttp(
                        datos_abiertos,
                        settings.ally_datos_abiertos_url,
                        settings.ally_datos_abiertos_credentials.client_secret,
                    ),
                },
                # W12-P05 la reemplaza por ProteccionDependencia.
                proteccion=SinProteccion(),
                timeouts_ms={
                    Aliado.OPEN_FINANCE: settings.ally_open_finance_timeout_ms,
                    Aliado.DATOS_ABIERTOS: settings.ally_datos_abiertos_timeout_ms,
                },
            )
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    app.include_router(consultas.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = crear_app()
