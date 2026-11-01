from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from solventa_common.telemetry import instrumentar

from bff_web.adapters.inbound import (
    aceptaciones,
    clientes,
    consentimientos,
    cotizaciones,
    operacion,
    perfilamiento,
    polizas,
    recaudos,
    socios,
    suscripciones,
    usuarios,
)
from bff_web.adapters.outbound.clientes import cerrar_compartidos
from bff_web.config import settings


@asynccontextmanager
async def ciclo(_: FastAPI) -> AsyncIterator[None]:
    yield
    await cerrar_compartidos()


app = instrumentar(FastAPI(title=settings.service_name, lifespan=ciclo), settings)
# El portal (CloudFront) y la API de canales tienen orígenes distintos; API Gateway
# reenvía el preflight al BFF (infra/platform/edge.tf).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", settings.correlation_header],
    expose_headers=[settings.correlation_header],
    # Chrome pide Private Network Access entre localhost:4200 y el BFF local; en AWS no aplica.
    allow_private_network=settings.environment == "local",
)
app.include_router(operacion.router)
app.include_router(usuarios.router)
app.include_router(socios.router)
app.include_router(perfilamiento.router)
# HU-W03: cálculo de la prima (cotizacion).
app.include_router(cotizaciones.router)
app.include_router(cotizaciones.router_productos)
app.include_router(clientes.router)
app.include_router(consentimientos.router)
# HU-W13: suscripción automática (polizas).
app.include_router(suscripciones.router)
app.include_router(suscripciones.router_bandeja)
# HU-W15: aceptación de la oferta (polizas).
app.include_router(aceptaciones.router)
# HU-W16: estado del recaudo de la prima aceptada (polizas).
app.include_router(recaudos.router)
# HU-W17: la póliza y la URL de su documento (polizas).
app.include_router(polizas.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
