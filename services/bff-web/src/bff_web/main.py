from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from solventa_common.telemetry import instrumentar

from bff_web.adapters.inbound import operacion, usuarios
from bff_web.config import settings

app = instrumentar(FastAPI(title=settings.service_name), settings)
# El portal (CloudFront) y la API de canales tienen orígenes distintos; API Gateway
# reenvía el preflight al BFF (infra/platform/edge.tf).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", settings.correlation_header],
    expose_headers=[settings.correlation_header],
    # Chrome pide Private Network Access entre localhost:4200 y el BFF local; en AWS no aplica.
    allow_private_network=settings.environment == "local",
)
app.include_router(operacion.router)
app.include_router(usuarios.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
