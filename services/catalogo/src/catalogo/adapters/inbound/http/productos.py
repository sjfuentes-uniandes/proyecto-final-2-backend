"""API /v1/productos: producto y reglas de prima versionadas (HU-W03). La consume cotizacion.

El producto se entrega tal como está (estado y vigencia): quien cotiza decide con esos datos en la
fecha de la cotización, y así el producto se guarda en caché por id. Las reglas sí se resuelven
por fecha: las vigentes son las de mayor vigenteDesde ≤ fecha.

HU-W05: las dos lecturas del recorrido de cotización respetan X-Deadline-Ms (el tiempo que
cotizacion seguirá esperando): cortan en timeout_efectivo_ms(consulta_timeout_ms, deadline) y
responden 504 TIEMPO_AGOTADO.

Los mismos DTO son el valor de la caché (`adapters/outbound/productos_cache.py`): cotizacion lee
de Valkey exactamente lo que respondería esta API.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio
from solventa_common.resiliencia import TiempoAgotado, acotar

from catalogo.adapters.inbound.http.esquema_prima import DefinicionReglasPrima
from catalogo.adapters.inbound.http.modelos_riesgo import PATRON_PRODUCTO, PATRON_VERSION, ErrorDto
from catalogo.application.productos.consultar_producto import ConsultarProductos
from catalogo.domain.productos.errores import ProductoNoEncontrado, ReglasNoVigentes
from catalogo.domain.productos.producto import EstadoProducto, Producto, ReglasPrima

router = APIRouter(prefix="/v1/productos", tags=["productos"])

ProductoId = Annotated[str, Path(alias="productoId", pattern=PATRON_PRODUCTO)]
DeadlineMs = Annotated[
    int | None,
    Header(
        alias="X-Deadline-Ms",
        ge=1,
        description="Milisegundos que quien llama seguirá esperando (contracts/encabezados.md)",
    ),
]
CONSULTA_TIMEOUT_MS = 500


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CoberturaDto(_Dto):
    codigo: str
    nombre: str
    obligatoria: bool


class ProductoRespuesta(_Dto):
    id: str
    version: str
    nombre: str
    ramo: str
    pais: str
    moneda: str
    estado: EstadoProducto
    vigente_desde: datetime
    vigente_hasta: datetime | None
    coberturas: list[CoberturaDto]

    @classmethod
    def desde(cls, producto: Producto) -> "ProductoRespuesta":
        return cls(
            id=producto.id,
            version=producto.version,
            nombre=producto.nombre,
            ramo=producto.ramo,
            pais=producto.pais,
            moneda=producto.moneda,
            estado=producto.estado,
            vigente_desde=producto.vigente_desde,
            vigente_hasta=producto.vigente_hasta,
            coberturas=[
                CoberturaDto(codigo=c.codigo, nombre=c.nombre, obligatoria=c.obligatoria)
                for c in producto.coberturas
            ],
        )


class ReglasPrimaRespuesta(_Dto):
    producto_id: str
    version: str
    vigente_desde: datetime
    hash: str
    definicion: DefinicionReglasPrima

    @classmethod
    def desde(cls, reglas: ReglasPrima) -> "ReglasPrimaRespuesta":
        return cls(
            producto_id=reglas.producto_id,
            version=reglas.version,
            vigente_desde=reglas.vigente_desde,
            hash=reglas.hash,
            definicion=DefinicionReglasPrima.model_validate(reglas.definicion),
        )


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


def _consultar(request: Request) -> ConsultarProductos:
    caso: ConsultarProductos = request.app.state.consultar_productos
    return caso


Consultar = Annotated[ConsultarProductos, Depends(_consultar)]


async def _con_deadline[T](
    request: Request, operacion: str, fn: Callable[[], Awaitable[T]], deadline_ms: int | None
) -> T:
    try:
        return await acotar(
            fn,
            operacion=operacion,
            proveedor="base-catalogo",
            configurado_ms=getattr(request.app.state, "consulta_timeout_ms", CONSULTA_TIMEOUT_MS),
            deadline_ms=deadline_ms,
        )
    except TiempoAgotado as error:
        raise ErrorNegocio(
            "TIEMPO_AGOTADO", "La consulta no terminó dentro del tiempo disponible", 504
        ) from error


def _producto_no_encontrado() -> ErrorNegocio:
    return ErrorNegocio("PRODUCTO_NO_ENCONTRADO", "El producto no existe", 404)


def _reglas_no_vigentes() -> ErrorNegocio:
    return ErrorNegocio("REGLAS_NO_VIGENTES", "El producto no tiene reglas de prima vigentes", 404)


@router.get(
    "/{productoId}",
    response_model=ProductoRespuesta,
    responses={
        404: _error("PRODUCTO_NO_ENCONTRADO"),
        422: _error("SOLICITUD_INVALIDA"),
        504: _error("TIEMPO_AGOTADO (se agotó X-Deadline-Ms o consulta_timeout_ms)"),
    },
)
async def producto(
    request: Request,
    consultar: Consultar,
    producto_id: ProductoId,
    x_deadline_ms: DeadlineMs = None,
) -> ProductoRespuesta:
    """El producto con su estado, su vigencia y sus coberturas."""
    try:
        encontrado = await _con_deadline(
            request, "catalogo.producto", lambda: consultar.producto(producto_id), x_deadline_ms
        )
        return ProductoRespuesta.desde(encontrado)
    except ProductoNoEncontrado as error:
        raise _producto_no_encontrado() from error


@router.get(
    "/{productoId}/reglas-prima/vigente",
    response_model=ReglasPrimaRespuesta,
    responses={
        404: _error("PRODUCTO_NO_ENCONTRADO o REGLAS_NO_VIGENTES (ninguna vigente en la fecha)"),
        422: _error("SOLICITUD_INVALIDA"),
        504: _error("TIEMPO_AGOTADO (se agotó X-Deadline-Ms o consulta_timeout_ms)"),
    },
)
async def reglas_vigentes(
    request: Request,
    consultar: Consultar,
    producto_id: ProductoId,
    fecha: Annotated[
        AwareDatetime | None,
        Query(description="Instante ISO-8601 con zona; por defecto, ahora"),
    ] = None,
    x_deadline_ms: DeadlineMs = None,
) -> ReglasPrimaRespuesta:
    """Las reglas de prima del producto con el mayor vigenteDesde ≤ fecha."""
    try:
        reglas = await _con_deadline(
            request,
            "catalogo.reglas_prima",
            lambda: consultar.reglas_vigentes(producto_id, fecha),
            x_deadline_ms,
        )
    except ProductoNoEncontrado as error:
        raise _producto_no_encontrado() from error
    except ReglasNoVigentes as error:
        raise _reglas_no_vigentes() from error
    return ReglasPrimaRespuesta.desde(reglas)


@router.get(
    "/{productoId}/reglas-prima/{version}",
    response_model=ReglasPrimaRespuesta,
    responses={
        404: _error("PRODUCTO_NO_ENCONTRADO o REGLAS_NO_VIGENTES (no existe la versión)"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def reglas_por_version(
    consultar: Consultar,
    producto_id: ProductoId,
    version: Annotated[str, Path(pattern=PATRON_VERSION)],
) -> ReglasPrimaRespuesta:
    """Una versión publicada de las reglas; las versiones no cambian nunca."""
    try:
        reglas = await consultar.reglas_por_version(producto_id, version)
    except ProductoNoEncontrado as error:
        raise _producto_no_encontrado() from error
    except ReglasNoVigentes as error:
        raise _reglas_no_vigentes() from error
    return ReglasPrimaRespuesta.desde(reglas)
