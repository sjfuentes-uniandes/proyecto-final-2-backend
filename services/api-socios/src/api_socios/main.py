from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from solventa_common.errors import registrar_manejadores
from solventa_common.telemetry import instrumentar

from api_socios.adapters.inbound import admin, socio
from api_socios.config import settings


@asynccontextmanager
async def ciclo(_: FastAPI) -> AsyncIterator[None]:
    # En ECS siempre hay DB_HOST; sin base configurada (pruebas N1/contrato) no se migra.
    if settings.db_host or settings.database_url_override:
        from api_socios.adapters.outbound.postgres import migrar

        migrar(settings.database_url)
    yield


app = instrumentar(FastAPI(title=settings.service_name, lifespan=ciclo), settings)
# Errores {codigo, mensaje, correlationId, detalles} (contracts/codigos-error.md).
registrar_manejadores(app)
app.include_router(admin.router)
app.include_router(socio.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
