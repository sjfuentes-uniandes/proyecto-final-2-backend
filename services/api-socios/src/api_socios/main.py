from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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
app.include_router(admin.router)
app.include_router(socio.router)


@app.exception_handler(RequestValidationError)
async def solicitud_invalida(_: Request, error: RequestValidationError) -> JSONResponse:
    # Contrato del API de socios: 400 para solicitudes mal formadas. Solo ubicación y
    # tipo de cada error: el valor recibido no se devuelve.
    return JSONResponse(
        status_code=400,
        content={"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in error.errors()]},
    )


# Cuerpo de todos los errores: texto (HTTPException) o lista de validaciones.
ERROR = {
    "title": "Error",
    "type": "object",
    "required": ["detail"],
    "properties": {"detail": {"anyOf": [
        {"type": "string"},
        {"type": "array", "items": {"type": "object", "required": ["loc", "msg", "type"],
                                    "properties": {"loc": {"type": "array"}, "msg": {"type": "string"},
                                                   "type": {"type": "string"}}}},
    ]}},
}


def _contrato() -> dict:
    """OpenAPI con 400 en vez del 422 de FastAPI y un esquema Error único (contracts/openapi/api-socios.yaml)."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    esquema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    contenido = {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}
    for operaciones in esquema["paths"].values():
        for operacion in operaciones.values():
            respuestas = operacion.get("responses", {})
            if "422" in respuestas:
                respuestas.pop("422")
                respuestas["400"] = {"description": "Solicitud inválida"}
            for codigo, respuesta in respuestas.items():
                if codigo.startswith(("4", "5")):
                    respuesta["content"] = contenido
    esquemas = esquema["components"]["schemas"]
    for sobrante in ("HTTPValidationError", "ValidationError"):
        esquemas.pop(sobrante, None)
    esquemas["Error"] = ERROR
    app.openapi_schema = esquema
    return esquema


app.openapi = _contrato  # type: ignore[method-assign]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
