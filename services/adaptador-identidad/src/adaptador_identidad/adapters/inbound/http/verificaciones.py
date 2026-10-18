"""API POST /v1/verificaciones: verifica la identidad de un cliente con el proveedor KYC."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio
from solventa_common.resiliencia import CORTE_DURO_MS, DependenciaNoDisponible, TiempoAgotado

from adaptador_identidad.application.verificaciones.verificar_documento import VerificarDocumento
from adaptador_identidad.application.verificaciones.verificar_identidad import VerificarIdentidad
from adaptador_identidad.domain.verificaciones.modelos import (
    DatosDocumento,
    RespuestaInvalida,
    ResultadoDocumento,
    ResultadoVerificacion,
)

router = APIRouter(prefix="/v1/verificaciones", tags=["verificaciones"])

# Igual que adaptador-datos: entero opcional y no anulable (evita que Schemathesis envíe "null").
DeadlineMs = Annotated[int, Header(alias="X-Deadline-Ms", ge=1, le=CORTE_DURO_MS)]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SolicitudVerificacion(_Dto):
    cliente_id: UUID
    selfie: Annotated[str, Field(min_length=1)]
    prueba_de_vida: dict[str, Any]


class VerificacionDto(_Dto):
    resultado: ResultadoVerificacion
    motivo: str | None


class SolicitudVerificacionDocumento(_Dto):
    cliente_id: UUID
    tipo_documento: Annotated[str, Field(min_length=1, max_length=10)]
    numero_documento: Annotated[str, Field(min_length=3, max_length=20)]
    nombre_completo: Annotated[str, Field(min_length=1, max_length=200)]
    fecha_nacimiento: date

    @field_validator("fecha_nacimiento", mode="before")
    @classmethod
    def _solo_texto_iso(cls, valor: object) -> object:
        # Pydantic acepta un entero como marca de tiempo: aquí solo vale "AAAA-MM-DD".
        if not isinstance(valor, str):
            raise ValueError("fechaNacimiento debe ser texto AAAA-MM-DD")  # noqa: TRY004
        return valor


class VerificacionDocumentoDto(_Dto):
    resultado: ResultadoDocumento
    motivo: str | None
    referencia: str
    proveedor: str


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


@contextmanager
def _traducir_errores_del_proveedor() -> Iterator[None]:
    try:
        yield
    except TiempoAgotado as error:
        raise ErrorNegocio("TIEMPO_AGOTADO", "El proveedor no respondió a tiempo", 504) from error
    except DependenciaNoDisponible as error:
        raise ErrorNegocio(
            "FUENTE_NO_DISPONIBLE",
            "El proveedor no está disponible",
            503,
            {"motivo": error.motivo, "circuito": error.circuito},
        ) from error
    except RespuestaInvalida as error:
        raise ErrorNegocio(
            "RESPUESTA_INVALIDA", "El proveedor respondió con un formato inválido", 502
        ) from error


_ERRORES_PROVEEDOR: dict[int | str, dict[str, Any]] = {
    400: _error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
    422: _error("SOLICITUD_INVALIDA"),
    502: _error("RESPUESTA_INVALIDA"),
    503: _error("FUENTE_NO_DISPONIBLE (detalles.motivo y detalles.circuito)"),
    504: _error("TIEMPO_AGOTADO"),
}


@router.post(
    "",
    response_model=VerificacionDto,
    response_model_by_alias=True,
    responses=_ERRORES_PROVEEDOR,
)
async def verificar(
    solicitud: SolicitudVerificacion,
    request: Request,
    x_deadline_ms: DeadlineMs = None,  # type: ignore[assignment]
) -> VerificacionDto:
    """Envía la selfie y la prueba de vida al proveedor KYC y devuelve el resultado."""
    caso: VerificarIdentidad = request.app.state.verificar_identidad
    with _traducir_errores_del_proveedor():
        verificacion = await caso.ejecutar(
            cliente_id=solicitud.cliente_id,
            selfie=solicitud.selfie,
            prueba_de_vida=solicitud.prueba_de_vida,
            deadline_ms=x_deadline_ms,
        )
    return VerificacionDto(resultado=verificacion.resultado, motivo=verificacion.motivo)


@router.post(
    "/documento",
    response_model=VerificacionDocumentoDto,
    response_model_by_alias=True,
    responses=_ERRORES_PROVEEDOR,
)
async def verificar_documento(
    solicitud: SolicitudVerificacionDocumento,
    request: Request,
    x_deadline_ms: DeadlineMs = None,  # type: ignore[assignment]
) -> VerificacionDocumentoDto:
    """Consulta al proveedor KYC con los datos del documento: no recibe ni guarda biometría."""
    caso: VerificarDocumento = request.app.state.verificar_documento
    datos = DatosDocumento(
        tipo_documento=solicitud.tipo_documento,
        numero_documento=solicitud.numero_documento,
        nombre_completo=solicitud.nombre_completo,
        fecha_nacimiento=solicitud.fecha_nacimiento,
    )
    with _traducir_errores_del_proveedor():
        verificacion = await caso.ejecutar(solicitud.cliente_id, datos, x_deadline_ms)
    return VerificacionDocumentoDto(
        resultado=verificacion.resultado,
        motivo=verificacion.motivo,
        referencia=verificacion.referencia,
        proveedor=verificacion.proveedor,
    )
