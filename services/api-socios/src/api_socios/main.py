from fastapi import FastAPI

from api_socios.config import settings

app = FastAPI(title=settings.service_name)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
