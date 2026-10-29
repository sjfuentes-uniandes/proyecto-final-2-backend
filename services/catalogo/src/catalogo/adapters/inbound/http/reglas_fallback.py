"""API /v1/productos/{productoId}/reglas-fallback (HU-W06). La consume cotizacion.

Mismo patrón que las reglas de suscripción (DS2-18): las vigentes se resuelven por fecha (las de
mayor vigenteDesde ≤ fecha, si no vencieron) y una versión publicada no cambia nunca. Sin POST: se
publican por semilla.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio

from catalogo.adapters.inbound.http.esquema_fallback import DefinicionReglasFallback
from catalogo.adapters.inbound.http.modelos_riesgo import PATRON_PRODUCTO, PATRON_VERSION, ErrorDto
from catalogo.application.fallback.consultar_reglas import ConsultarReglasFallback
from catalogo.domain.fallback.errores import ReglasFallbackNoVigentes
from catalogo.domain.fallback.reglas import ReglasFallback
from catalogo.domain.productos.errores import ProductoNoEncontrado

router = APIRouter(prefix="/v1/productos", tags=["reglas-fallback"])

ProductoId = Annotated[str, Path(alias="productoId", pattern=PATRON_PRODUCTO)]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ReglasFallbackRespuesta(_Dto):
    producto_id: str
    version: str
    vigente_desde: datetime
    vigente_hasta: datetime | None
    hash: str
    definicion: DefinicionReglasFallback

    @classmethod
    def desde(cls, reglas: ReglasFallback) -> "ReglasFallbackRespuesta":
        return cls(
            producto_id=reglas.producto_id,
            version=reglas.version,
            vigente_desde=reglas.vigente_desde,
            vigente_hasta=reglas.vigente_hasta,
            hash=reglas.hash,
            definicion=DefinicionReglasFallback.model_validate(reglas.definicion),
        )


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


def _consultar(request: Request) -> ConsultarReglasFallback:
    caso: ConsultarReglasFallback = request.app.state.consultar_reglas_fallback
    return caso


Consultar = Annotated[ConsultarReglasFallback, Depends(_consultar)]


def _traducir(error: Exception) -> ErrorNegocio:
    if isinstance(error, ProductoNoEncontrado):
        return ErrorNegocio("PRODUCTO_NO_ENCONTRADO", "El producto no existe", 404)
    return ErrorNegocio(
        "REGLAS_NO_VIGENTES", "El producto no tiene reglas de fallback vigentes", 404
    )


@router.get(
    "/{productoId}/reglas-fallback/vigente",
    response_model=ReglasFallbackRespuesta,
    responses={
        404: _error("PRODUCTO_NO_ENCONTRADO o REGLAS_NO_VIGENTES (ninguna vigente en la fecha)"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def reglas_fallback_vigentes(
    consultar: Consultar,
    producto_id: ProductoId,
    fecha: Annotated[
        AwareDatetime | None,
        Query(description="Instante ISO-8601 con zona; por defecto, ahora"),
    ] = None,
) -> ReglasFallbackRespuesta:
    """Las reglas de fallback del producto con el mayor vigenteDesde ≤ fecha, si no vencieron
    (HU-W06). Una fuente sin regla aquí no es degradable."""
    try:
        reglas = await consultar.vigentes(producto_id, fecha)
    except (ProductoNoEncontrado, ReglasFallbackNoVigentes) as error:
        raise _traducir(error) from error
    return ReglasFallbackRespuesta.desde(reglas)


@router.get(
    "/{productoId}/reglas-fallback/{version}",
    response_model=ReglasFallbackRespuesta,
    responses={
        404: _error("PRODUCTO_NO_ENCONTRADO o REGLAS_NO_VIGENTES (no existe la versión)"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def reglas_fallback_por_version(
    consultar: Consultar,
    producto_id: ProductoId,
    version: Annotated[str, Path(pattern=PATRON_VERSION)],
) -> ReglasFallbackRespuesta:
    """Una versión publicada de las reglas de fallback; no cambia nunca (explicación, AC3)."""
    try:
        reglas = await consultar.por_version(producto_id, version)
    except (ProductoNoEncontrado, ReglasFallbackNoVigentes) as error:
        raise _traducir(error) from error
    return ReglasFallbackRespuesta.desde(reglas)
