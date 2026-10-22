"""Cache-Aside del producto y de sus reglas de prima en Valkey (HU-W03).

Según los diagramas (modelo_componentes, modelo_despliegue y modelo_informacion), catalogo
actualiza e invalida la caché y cotizacion solo la lee. Este decorador del repositorio la llena
cuando una lectura no la encuentra y borra las claves de los productos al arrancar (una semilla o
un despliegue nuevo no deja datos viejos más allá del arranque).

Claves (contrato con cotizacion, contracts/README.md §Caché de catálogo). El valor es el JSON que
respondería la API:

- `catalogo:v1:producto:{id}`: `ProductoRespuesta`, `ttl_s`.
- `catalogo:v1:reglas-prima:{id}:vigente`: `ReglasPrimaRespuesta` de las reglas vigentes ahora,
  `ttl_s`. Solo se usa para fechas a menos de `ttl_s` de ahora: una consulta histórica va a la base.
- `catalogo:v1:reglas-prima:{id}:version:{version}`: una versión cerrada no cambia; 24 h.

Con `ttl_s = 0` no se lee ni se escribe.
"""

from datetime import datetime, timedelta

from pydantic import ValidationError
from solventa_common.cache import Cache

from catalogo.adapters.inbound.http.esquema_prima import a_dict
from catalogo.adapters.inbound.http.productos import ProductoRespuesta, ReglasPrimaRespuesta
from catalogo.adapters.outbound.productos_pg import producto_de
from catalogo.application.productos.puertos import RepositorioProductos
from catalogo.application.reglas.puertos import Reloj
from catalogo.domain.productos.producto import Producto, ReglasPrima

PREFIJO = "catalogo:v1"
TTL_VERSION_S = 24 * 3600


def clave_producto(producto_id: str) -> str:
    return f"{PREFIJO}:producto:{producto_id}"


def clave_reglas_vigentes(producto_id: str) -> str:
    return f"{PREFIJO}:reglas-prima:{producto_id}:vigente"


def clave_reglas_version(producto_id: str, version: str) -> str:
    return f"{PREFIJO}:reglas-prima:{producto_id}:version:{version}"


def _producto_de(guardado: bytes | None) -> Producto | None:
    """El producto guardado; None si no hay o si el valor es ajeno al contrato (se reemplaza)."""
    if guardado is None:
        return None
    try:
        dto = ProductoRespuesta.model_validate_json(guardado)
    except ValidationError:
        return None
    return producto_de(dto.model_dump(mode="json", by_alias=True))


def _reglas_de(dto: ReglasPrimaRespuesta) -> ReglasPrima:
    return ReglasPrima(
        producto_id=dto.producto_id,
        version=dto.version,
        vigente_desde=dto.vigente_desde,
        definicion=a_dict(dto.definicion),
        hash=dto.hash,
        publicado_por="cache",
    )


class ProductosConCache:
    """Implementa RepositorioProductos sobre otro repositorio y la caché."""

    def __init__(self, repo: RepositorioProductos, cache: Cache, reloj: Reloj, ttl_s: int) -> None:
        self._repo = repo
        self._cache = cache
        self._reloj = reloj
        self._ttl_s = ttl_s

    async def producto(self, producto_id: str) -> Producto | None:
        clave = clave_producto(producto_id)
        if self._ttl_s > 0 and (producto := _producto_de(await self._cache.leer(clave))):
            return producto
        producto = await self._repo.producto(producto_id)
        if producto is not None and self._ttl_s > 0:
            valor = ProductoRespuesta.desde(producto).model_dump_json(by_alias=True)
            await self._cache.escribir(clave, valor.encode(), self._ttl_s)
        return producto

    async def reglas_vigentes(self, producto_id: str, en: datetime) -> ReglasPrima | None:
        clave = clave_reglas_vigentes(producto_id)
        usar_cache = self._ttl_s > 0 and abs(en - self._reloj.ahora()) <= timedelta(
            seconds=self._ttl_s
        )
        if usar_cache:
            reglas = await self._leer_reglas(clave)
            if reglas is not None and reglas.vigente_desde <= en:
                return reglas
        reglas = await self._repo.reglas_vigentes(producto_id, en)
        if reglas is not None and usar_cache:
            await self._escribir_reglas(clave, reglas, self._ttl_s)
        return reglas

    async def reglas_por_version(self, producto_id: str, version: str) -> ReglasPrima | None:
        clave = clave_reglas_version(producto_id, version)
        if self._ttl_s > 0 and (reglas := await self._leer_reglas(clave)) is not None:
            return reglas
        reglas = await self._repo.reglas_por_version(producto_id, version)
        if reglas is not None and self._ttl_s > 0:
            await self._escribir_reglas(clave, reglas, TTL_VERSION_S)
        return reglas

    async def invalidar(self, productos: list[str]) -> None:
        """Borra el producto y sus reglas vigentes de la caché (las versiones no cambian)."""
        claves = [c for p in productos for c in (clave_producto(p), clave_reglas_vigentes(p))]
        await self._cache.borrar(*claves)

    async def _leer_reglas(self, clave: str) -> ReglasPrima | None:
        guardado = await self._cache.leer(clave)
        if guardado is None:
            return None
        try:
            return _reglas_de(ReglasPrimaRespuesta.model_validate_json(guardado))
        except ValidationError:
            return None

    async def _escribir_reglas(self, clave: str, reglas: ReglasPrima, ttl_s: int) -> None:
        valor = ReglasPrimaRespuesta.desde(reglas).model_dump_json(by_alias=True)
        await self._cache.escribir(clave, valor.encode(), ttl_s)
