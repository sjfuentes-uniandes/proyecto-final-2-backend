"""API /v1/firmas: firma la huella del documento de la póliza (HU-W17).

Solo la llama `polizas`. Se firma la huella sha256 del PDF, no el PDF (ADR 0014, decisión 5); el
firmante es una referencia sintética, sin datos personales.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio
from solventa_common.resiliencia import DependenciaNoDisponible, TiempoAgotado

from adaptador_firma.application.firmas.firmar import Firmar
from adaptador_firma.domain.firmas.modelos import EstadoFirma, Firma, RespuestaInvalida
from adaptador_firma.domain.firmas.modelos import SolicitudFirma as SolicitudFirmaDominio

router = APIRouter(prefix="/v1/firmas", tags=["firmas"])

# [0-9a-f] y no \d ni mayúsculas: la huella es un texto exacto que el proveedor debe devolver igual.
Huella = Annotated[
    str, Field(pattern=r"^sha256:[0-9a-f]{64}$", examples=["sha256:" + "0123456789abcdef" * 4])
]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SolicitudFirma(_Dto):
    documento_id: UUID
    huella: Huella
    firmante_ref: UUID


class FirmaDto(_Dto):
    documento_id: UUID
    estado: EstadoFirma
    firma_id: str
    sello_tiempo: datetime
    huella: str

    @classmethod
    def de(cls, firma: Firma) -> "FirmaDto":
        return cls(
            documento_id=firma.documento_id,
            estado=firma.estado,
            firma_id=firma.firma_id,
            sello_tiempo=firma.sello_tiempo,
            huella=firma.huella,
        )


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


@contextmanager
def _traducir_errores_del_proveedor() -> Iterator[None]:
    # Ninguno es un rechazo: la emisión sigue EMITIENDO y `polizas` reintenta (ADR 0014).
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


@router.post(
    "",
    response_model=FirmaDto,
    response_model_by_alias=True,
    responses={
        400: _error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        422: _error("SOLICITUD_INVALIDA"),
        502: _error(
            "RESPUESTA_INVALIDA (también un 404, un estado no previsto o la firma de otro "
            "documento u otra huella)"
        ),
        503: _error("FUENTE_NO_DISPONIBLE (detalles.motivo y detalles.circuito)"),
        504: _error("TIEMPO_AGOTADO"),
    },
)
async def firmar(solicitud: SolicitudFirma, request: Request) -> FirmaDto:
    """Firma la huella con `documentoId` como clave idempotente hacia el proveedor."""
    caso: Firmar = request.app.state.firmar
    with _traducir_errores_del_proveedor():
        firma = await caso.ejecutar(
            SolicitudFirmaDominio(
                documento_id=solicitud.documento_id,
                huella=solicitud.huella,
                firmante_ref=solicitud.firmante_ref,
            )
        )
    return FirmaDto.de(firma)
