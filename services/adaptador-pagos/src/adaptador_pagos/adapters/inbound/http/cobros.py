"""API /v1/cobros: cobra el primer recaudo de la prima y consulta su estado (HU-W16).

Solo la llama `dinero`. Ningún dato de tarjeta ni de cuenta: el pagador es una referencia
sintética (ADR 0013, decisión 6).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio
from solventa_common.resiliencia import DependenciaNoDisponible, TiempoAgotado

from adaptador_pagos.application.cobros.cobrar import Cobrar
from adaptador_pagos.application.cobros.consultar_cobro import ConsultarCobro
from adaptador_pagos.domain.cobros.modelos import (
    Cobro,
    CobroNoEncontrado,
    EstadoCobro,
    RespuestaInvalida,
)
from adaptador_pagos.domain.cobros.modelos import (
    SolicitudCobro as SolicitudCobroDominio,
)

router = APIRouter(prefix="/v1/cobros", tags=["cobros"])

ClaveRecaudo = Annotated[UUID, Path(alias="claveRecaudo")]
PagadorRef = Annotated[UUID, Query(alias="pagadorRef")]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SolicitudCobro(_Dto):
    clave_recaudo: UUID
    # Texto decimal, nunca número (CLAUDE.md): [0-9] y no \d, que admite dígitos Unicode.
    monto: Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]{1,2})?$", examples=["64638.00"])]
    moneda: Literal["COP"]
    pagador_ref: UUID

    @field_validator("monto")
    @classmethod
    def _mayor_que_cero(cls, valor: str) -> str:
        if Decimal(valor) <= 0:
            raise ValueError("monto debe ser mayor que 0")
        return valor


class CobroDto(_Dto):
    clave_recaudo: UUID
    estado: EstadoCobro
    transaccion_id: str | None
    motivo: str | None

    @classmethod
    def de(cls, cobro: Cobro) -> "CobroDto":
        return cls(
            clave_recaudo=cobro.clave_recaudo,
            estado=cobro.estado,
            transaccion_id=cobro.transaccion_id,
            motivo=cobro.motivo,
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
    # Ninguno es un rechazo: `dinero` deja el movimiento PENDIENTE (ADR 0013, decisión 5).
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
    422: _error("SOLICITUD_INVALIDA"),
    502: _error("RESPUESTA_INVALIDA (también un 404 o un estado no previsto del proveedor)"),
    503: _error("FUENTE_NO_DISPONIBLE (detalles.motivo y detalles.circuito)"),
    504: _error("TIEMPO_AGOTADO"),
}


@router.post(
    "",
    response_model=CobroDto,
    response_model_by_alias=True,
    responses={
        400: _error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        **_ERRORES_PROVEEDOR,
    },
)
async def cobrar(solicitud: SolicitudCobro, request: Request) -> CobroDto:
    """Cobra con `claveRecaudo` como clave idempotente hacia el proveedor."""
    caso: Cobrar = request.app.state.cobrar
    with _traducir_errores_del_proveedor():
        cobro = await caso.ejecutar(
            SolicitudCobroDominio(
                clave_recaudo=solicitud.clave_recaudo,
                monto=Decimal(solicitud.monto),
                moneda=solicitud.moneda,
                pagador_ref=solicitud.pagador_ref,
            )
        )
    return CobroDto.de(cobro)


@router.get(
    "/{claveRecaudo}",
    response_model=CobroDto,
    response_model_by_alias=True,
    responses={
        404: _error("COBRO_NO_ENCONTRADO: el proveedor no tiene ese cobro"),
        **_ERRORES_PROVEEDOR,
    },
)
async def consultar(
    clave_recaudo: ClaveRecaudo, pagador_ref: PagadorRef, request: Request
) -> CobroDto:
    """Consulta el estado del cobro en el proveedor, para conciliar un movimiento pendiente."""
    caso: ConsultarCobro = request.app.state.consultar_cobro
    with _traducir_errores_del_proveedor():
        try:
            cobro = await caso.ejecutar(clave_recaudo, pagador_ref)
        except CobroNoEncontrado as error:
            raise ErrorNegocio(
                "COBRO_NO_ENCONTRADO", "El proveedor no tiene un cobro con esa clave", 404
            ) from error
    return CobroDto.de(cobro)
