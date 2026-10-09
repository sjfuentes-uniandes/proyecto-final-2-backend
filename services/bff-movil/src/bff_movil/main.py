from fastapi import FastAPI
from solventa_common.telemetry import instrumentar

from bff_movil.config import settings

app = instrumentar(FastAPI(title=settings.service_name), settings)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
