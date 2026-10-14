from typing import Any

from pydantic import BaseModel
from solventa_common.errors import ErrorNegocio

NO_AUTENTICADO = "NO_AUTENTICADO"
PERMISO_NO_ASIGNADO = "PERMISO_NO_ASIGNADO"
SOCIO_NO_ENCONTRADO = "SOCIO_NO_ENCONTRADO"
NO_ENCONTRADO = "NO_ENCONTRADO"
SOLICITUD_INVALIDA = "SOLICITUD_INVALIDA"
OPERACION_NO_DISPONIBLE = "OPERACION_NO_DISPONIBLE"


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


def no_autenticado() -> ErrorNegocio:
    return ErrorNegocio(
        NO_AUTENTICADO, "Credenciales inexistentes, incorrectas o desactivadas", 401
    )


def socio_no_encontrado() -> ErrorNegocio:
    return ErrorNegocio(SOCIO_NO_ENCONTRADO, "El socio no existe", 404)


def no_encontrado() -> ErrorNegocio:
    # Igual al 404 global: no revela que la ruta existe.
    return ErrorNegocio(NO_ENCONTRADO, "Recurso no encontrado", 404)
