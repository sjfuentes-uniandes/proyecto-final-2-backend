"""API /v1/productos/{productoId}/reglas-suscripcion (HU-W13). La consume polizas.

Mismo patrón que las reglas de prima (DS2-18): las vigentes se resuelven por fecha (las de mayor
vigenteDesde ≤ fecha) y una versión publicada no cambia nunca. Sin POST: en el Sprint 2 las reglas
se publican por semilla.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio

from catalogo.adapters.inbound.http.esquema_suscripcion import DefinicionReglasSuscripcion
from catalogo.adapters.inbound.http.modelos_riesgo import PATRON_PRODUCTO, PATRON_VERSION, ErrorDto
from catalogo.application.suscripcion.consultar_reglas import ConsultarReglasSuscripcion
from catalogo.domain.productos.errores import ProductoNoEncontrado
from catalogo.domain.suscripcion.errores import ReglasSuscripcionNoVigentes
from catalogo.domain.suscripcion.reglas import ReglasSuscripcion

router = APIRouter(prefix="/v1/productos", tags=["reglas-suscripcion"])

ProductoId = Annotated[str, Path(alias="productoId", pattern=PATRON_PRODUCTO)]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ReglasSuscripcionRespuesta(_Dto):
    producto_id: str
    version: str
    vigente_desde: datetime
    hash: str
    definicion: DefinicionReglasSuscripcion

    @classmethod
    def desde(cls, reglas: ReglasSuscripcion) -> "ReglasSuscripcionRespuesta":
        return cls(
            producto_id=reglas.producto_id,
            version=reglas.version,
            vigente_desde=reglas.vigente_desde,
            hash=reglas.hash,
            definicion=DefinicionReglasSuscripcion.model_validate(reglas.definicion),
        )


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


def _consultar(request: Request) -> ConsultarReglasSuscripcion:
    caso: ConsultarReglasSuscripcion = request.app.state.consultar_reglas_suscripcion
    return caso


Consultar = Annotated[ConsultarReglasSuscripcion, Depends(_consultar)]


def _traducir(error: Exception) -> ErrorNegocio:
    if isinstance(error, ProductoNoEncontrado):
        return ErrorNegocio("PRODUCTO_NO_ENCONTRADO", "El producto no existe", 404)
    return ErrorNegocio(
        "REGLAS_NO_VIGENTES", "El producto no tiene reglas de suscripción vigentes", 404
    )


@router.get(
    "/{productoId}/reglas-suscripcion/vigente",
    response_model=ReglasSuscripcionRespuesta,
    responses={
        404: _error("PRODUCTO_NO_ENCONTRADO o REGLAS_NO_VIGENTES (ninguna vigente en la fecha)"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def reglas_suscripcion_vigentes(
    consultar: Consultar,
    producto_id: ProductoId,
    fecha: Annotated[
        AwareDatetime | None,
        Query(description="Instante ISO-8601 con zona; por defecto, ahora"),
    ] = None,
) -> ReglasSuscripcionRespuesta:
    """Las reglas de suscripción del producto con el mayor vigenteDesde ≤ fecha (HU-W13)."""
    try:
        reglas = await consultar.vigentes(producto_id, fecha)
    except (ProductoNoEncontrado, ReglasSuscripcionNoVigentes) as error:
        raise _traducir(error) from error
    return ReglasSuscripcionRespuesta.desde(reglas)


@router.get(
    "/{productoId}/reglas-suscripcion/{version}",
    response_model=ReglasSuscripcionRespuesta,
    responses={
        404: _error("PRODUCTO_NO_ENCONTRADO o REGLAS_NO_VIGENTES (no existe la versión)"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def reglas_suscripcion_por_version(
    consultar: Consultar,
    producto_id: ProductoId,
    version: Annotated[str, Path(pattern=PATRON_VERSION)],
) -> ReglasSuscripcionRespuesta:
    """Una versión publicada de las reglas de suscripción; no cambia nunca (reconstrucción)."""
    try:
        reglas = await consultar.por_version(producto_id, version)
    except (ProductoNoEncontrado, ReglasSuscripcionNoVigentes) as error:
        raise _traducir(error) from error
    return ReglasSuscripcionRespuesta.desde(reglas)
