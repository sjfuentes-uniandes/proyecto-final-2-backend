"""Piezas compartidas por los routers de `clientes`: DTO base, error estable, actor y tope."""

from typing import Annotated, Any

from fastapi import Depends, Header
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from solventa_common.errors import ROL_NO_AUTORIZADO, ErrorNegocio

ROL_CLIENTE = "CLIENTE"
# Tope de la selfie en base64 (~3,7 MB de imagen): por debajo del límite del API Gateway.
MAX_SELFIE = 5_000_000


def actor_cliente(
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_rol: Annotated[str | None, Header(alias="X-Actor-Rol")] = None,
) -> str:
    """`sub` del titular. Se resuelve antes de validar el cuerpo: 401/403 van primero."""
    if not x_actor_id or not x_actor_rol:
        raise ErrorNegocio("ACTOR_REQUERIDO", "Faltan los encabezados del actor", 401)
    if x_actor_rol != ROL_CLIENTE:
        raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
    return x_actor_id


Titular = Annotated[str, Depends(actor_cliente)]


class Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


# Clave `int | str`: es el tipo de `responses` de FastAPI, donde se combina con más respuestas.
ERRORES_ACTOR: dict[int | str, dict[str, Any]] = {
    401: error("ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
    403: error("ROL_NO_AUTORIZADO: X-Actor-Rol distinto de CLIENTE"),
}
