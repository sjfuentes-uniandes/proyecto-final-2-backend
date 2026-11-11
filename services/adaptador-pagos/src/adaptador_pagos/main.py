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

from adaptador_pagos.adapters.inbound.http import cobros, desembolsos
from adaptador_pagos.adapters.outbound.desembolsos_http import DesembolsosHttp
from adaptador_pagos.adapters.outbound.pagos_http import PagosHttp
from adaptador_pagos.application.cobros.cobrar import Cobrar
from adaptador_pagos.application.cobros.consultar_cobro import ConsultarCobro
from adaptador_pagos.application.desembolsos.consultar_desembolso import ConsultarDesembolso
from adaptador_pagos.application.desembolsos.desembolsar import Desembolsar
from adaptador_pagos.config import Settings


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
            cliente_pagos = await pila.enter_async_context(
                correlation.cliente_async(transport=transport)
            )
            proveedor = PagosHttp(
                cliente_pagos,
                settings.ally_pagos_url,
                settings.ally_pagos_credentials.client_secret,
            )
            # Solo timeout y sin reintentos: la incertidumbre la resuelven dinero y la
            # conciliación (ADR 0013). El corte es el timeout del proveedor (5 s en infra): un
            # cobro no cabe en los 700 ms de las consultas de perfilamiento.
            proteccion = SinProteccion(corte_duro_ms=settings.ally_pagos_timeout_ms)
            app.state.cobrar = Cobrar(
                proveedor=proveedor,
                proteccion=proteccion,
                timeout_ms=settings.ally_pagos_timeout_ms,
            )
            app.state.consultar_cobro = ConsultarCobro(
                proveedor=proveedor,
                proteccion=proteccion,
                timeout_ms=settings.ally_pagos_timeout_ms,
            )
            # HU-W23: los desembolsos, con el mismo cliente, la misma credencial y el mismo corte.
            desembolsos_http = DesembolsosHttp(
                cliente_pagos,
                settings.ally_pagos_url,
                settings.ally_pagos_credentials.client_secret,
            )
            app.state.desembolsar = Desembolsar(
                proveedor=desembolsos_http,
                proteccion=proteccion,
                timeout_ms=settings.ally_pagos_timeout_ms,
            )
            app.state.consultar_desembolso = ConsultarDesembolso(
                proveedor=desembolsos_http,
                proteccion=proteccion,
                timeout_ms=settings.ally_pagos_timeout_ms,
            )
            yield

    app = instrumentar(
        FastAPI(title=settings.service_name, lifespan=ciclo),
        settings,
        span_exporter=span_exporter,
        metric_readers=metric_readers,
    )
    registrar_manejadores(app)
    app.include_router(cobros.router)
    app.include_router(desembolsos.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = crear_app()
