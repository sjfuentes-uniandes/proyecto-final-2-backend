from fastapi import FastAPI

from adaptador_datos.config import settings

app = FastAPI(title=settings.service_name)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
