"""Errores con respuesta estable {codigo, mensaje, correlationId, detalles} (regla 19).

`registrar_manejadores` aplica el formato a toda la app; `RutaB2` lo aplica solo al
router que la usa (bff-web conserva {"detail"} en las rutas de HU-W27).
"""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from solventa_common import correlation

SOLICITUD_INVALIDA = "SOLICITUD_INVALIDA"
NO_AUTENTICADO = "NO_AUTENTICADO"
ROL_NO_AUTORIZADO = "ROL_NO_AUTORIZADO"
NO_ENCONTRADO = "NO_ENCONTRADO"
METODO_NO_PERMITIDO = "METODO_NO_PERMITIDO"
ERROR_HTTP = "ERROR_HTTP"
ERROR_INTERNO = "ERROR_INTERNO"

_PREFIJOS_UBICACION = {"body", "query", "header", "path", "cookie"}
_MENSAJES = {
    SOLICITUD_INVALIDA: "Solicitud inválida",
    NO_AUTENTICADO: "No autenticado",
    ROL_NO_AUTORIZADO: "No autorizado",
    NO_ENCONTRADO: "Recurso no encontrado",
    METODO_NO_PERMITIDO: "Método no permitido",
    ERROR_HTTP: "Error en la solicitud",
    ERROR_INTERNO: "Error interno",
}
# Traducción por código HTTP dentro de RutaB2 (DT-16).
_CODIGOS_RUTA_B2 = {
    400: SOLICITUD_INVALIDA,
    401: NO_AUTENTICADO,
    403: ROL_NO_AUTORIZADO,
    404: NO_ENCONTRADO,
}
_CODIGOS_GLOBALES = {404: NO_ENCONTRADO, 405: METODO_NO_PERMITIDO}

logger = logging.getLogger("solventa.errores")


class ErrorNegocio(Exception):
    """Error funcional con código estable; el manejador responde con su estado HTTP."""

    def __init__(
        self,
        codigo: str,
        mensaje: str,
        estado_http: int,
        detalles: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(codigo)
        self.codigo = codigo
        self.mensaje = mensaje
        self.estado_http = estado_http
        self.detalles = detalles


def cuerpo_error(
    codigo: str, mensaje: str, detalles: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "codigo": codigo,
        "mensaje": mensaje,
        "correlationId": correlation.obtener(),
        "detalles": dict(detalles or {}),
    }


def _respuesta(
    estado: int,
    codigo: str,
    mensaje: str | None = None,
    detalles: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        cuerpo_error(codigo, mensaje or _MENSAJES.get(codigo, _MENSAJES[ERROR_HTTP]), detalles),
        status_code=estado,
        headers=dict(headers) if headers else None,
    )


def campos_invalidos(errores: Sequence[Mapping[str, Any]]) -> list[str]:
    """Rutas de los campos inválidos, sin el prefijo de ubicación y nunca con sus valores."""
    campos: list[str] = []
    for error in errores:
        ubicacion = [str(parte) for parte in error.get("loc", ())]
        if len(ubicacion) > 1 and ubicacion[0] in _PREFIJOS_UBICACION:
            ubicacion = ubicacion[1:]
        campo = ".".join(ubicacion)
        if campo not in campos:
            campos.append(campo)
    return campos


def _validacion(error: RequestValidationError) -> JSONResponse:
    return _respuesta(
        422, SOLICITUD_INVALIDA, detalles={"campos": campos_invalidos(error.errors())}
    )


def _negocio(error: ErrorNegocio) -> JSONResponse:
    return _respuesta(error.estado_http, error.codigo, error.mensaje, error.detalles)


async def _manejar_negocio(_: Request, error: Exception) -> Response:
    assert isinstance(error, ErrorNegocio)
    return _negocio(error)


async def _manejar_validacion(_: Request, error: Exception) -> Response:
    assert isinstance(error, RequestValidationError)
    return _validacion(error)


async def _manejar_http(_: Request, error: Exception) -> Response:
    assert isinstance(error, StarletteHTTPException)
    codigo = _CODIGOS_GLOBALES.get(error.status_code, ERROR_HTTP)
    return _respuesta(error.status_code, codigo, headers=error.headers)


async def _manejar_inesperado(_: Request, error: Exception) -> Response:
    # Solo el tipo: el mensaje puede contener datos del cliente.
    logger.error("error no controlado", extra={"error_type": type(error).__name__})
    return _respuesta(500, ERROR_INTERNO)


def registrar_manejadores(app: FastAPI) -> None:
    """Formato B2 para los errores de toda la app."""
    app.add_exception_handler(ErrorNegocio, _manejar_negocio)
    app.add_exception_handler(RequestValidationError, _manejar_validacion)
    app.add_exception_handler(StarletteHTTPException, _manejar_http)
    app.add_exception_handler(Exception, _manejar_inesperado)


class RutaB2(APIRoute):
    """Ruta que responde sus errores en formato B2 sin tocar los manejadores globales."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()

        async def manejar(request: Request) -> Response:
            try:
                return await original(request)
            except ErrorNegocio as error:
                return _negocio(error)
            except RequestValidationError as error:
                return _validacion(error)
            except HTTPException as error:
                codigo = _CODIGOS_RUTA_B2.get(error.status_code, ERROR_HTTP)
                return _respuesta(error.status_code, codigo, headers=error.headers)

        return manejar
