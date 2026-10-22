"""Consulta del producto y de sus reglas de prima (HU-W03)."""

from datetime import datetime

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from catalogo.application.productos.puertos import RepositorioProductos
from catalogo.application.reglas.puertos import Reloj
from catalogo.domain.productos.errores import ProductoNoEncontrado, ReglasNoVigentes
from catalogo.domain.productos.producto import Producto, ReglasPrima


class ConsultarProductos:
    def __init__(self, repo: RepositorioProductos, reloj: Reloj) -> None:
        self._repo = repo
        self._reloj = reloj

    async def producto(self, producto_id: str) -> Producto:
        with operacion("producto.consultar") as op:
            producto = await self._repo.producto(producto_id)
            if producto is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise ProductoNoEncontrado()
        return producto

    async def reglas_vigentes(self, producto_id: str, en: datetime | None) -> ReglasPrima:
        with operacion("reglas_prima.consultar") as op:
            reglas = await self._repo.reglas_vigentes(producto_id, en or self._reloj.ahora())
            return await self._encontradas(producto_id, reglas, op)

    async def reglas_por_version(self, producto_id: str, version: str) -> ReglasPrima:
        with operacion("reglas_prima.consultar") as op:
            reglas = await self._repo.reglas_por_version(producto_id, version)
            return await self._encontradas(producto_id, reglas, op)

    async def _encontradas(
        self, producto_id: str, reglas: ReglasPrima | None, op: operacion
    ) -> ReglasPrima:
        """Sin reglas, distingue el producto inexistente de las reglas que faltan."""
        if reglas is not None:
            return reglas
        op.marcar(RESULTADO_RECHAZADO)
        if await self._repo.producto(producto_id) is None:
            raise ProductoNoEncontrado()
        raise ReglasNoVigentes()
