import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from solventa_common.errors import registrar_manejadores
from solventa_common.telemetry import instrumentar

from api_socios.adapters.inbound import admin, socio
from api_socios.config import settings


@asynccontextmanager
async def ciclo(_: FastAPI) -> AsyncIterator[None]:
    # Sin base configurada (pruebas) no se migra.
    if settings.db_host or settings.database_url_override:
        from api_socios.adapters.outbound.postgres import migrar

        migrar(settings.database_url)
    tarea = None
    if (
        settings.quota_metric_interval_s > 0
        and settings.partners_user_pool_id
        and settings.partners_usage_plans
    ):
        from api_socios.adapters.inbound.cuotas_periodicas import publicar_periodicamente
        from api_socios.adapters.inbound.dependencias import publicar_cuotas

        tarea = asyncio.create_task(
            publicar_periodicamente(publicar_cuotas, settings.quota_metric_interval_s)
        )
    yield
    if tarea is not None:
        tarea.cancel()
        with suppress(asyncio.CancelledError):
            await tarea


app = instrumentar(FastAPI(title=settings.service_name, lifespan=ciclo), settings)
registrar_manejadores(app)
app.include_router(admin.router)
app.include_router(socio.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
