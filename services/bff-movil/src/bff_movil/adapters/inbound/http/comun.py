"""Piezas compartidas por los routers del BFF: DTO base, error estable y cabeceras."""

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

CABECERAS = {
    "X-Correlation-Id": {
        "description": "Identificador de correlación de la solicitud",
        "schema": {"type": "string"},
    }
}


class Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion, "headers": CABECERAS}
