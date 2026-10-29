"""Consulta de las reglas de fallback vigentes o de una versión (HU-W06).

Las consume `cotizacion`: las vigentes para decidir si una fuente faltante permite una oferta
degradada, y una versión cerrada para explicar una decisión ya tomada.
"""

from datetime import datetime

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from catalogo.application.fallback.puertos import RepositorioReglasFallback
from catalogo.application.productos.puertos import RepositorioProductos
from catalogo.application.reglas.puertos import Reloj
from catalogo.domain.fallback.errores import ReglasFallbackNoVigentes
from catalogo.domain.fallback.reglas import ReglasFallback
from catalogo.domain.productos.errores import ProductoNoEncontrado


class ConsultarReglasFallback:
    def __init__(
        self, repo: RepositorioReglasFallback, productos: RepositorioProductos, reloj: Reloj
    ) -> None:
        self._repo = repo
        self._productos = productos
        self._reloj = reloj

    async def vigentes(self, producto_id: str, en: datetime | None) -> ReglasFallback:
        with operacion("reglas_fallback.consultar") as op:
            reglas = await self._repo.vigentes(producto_id, en or self._reloj.ahora())
            return await self._encontradas(producto_id, reglas, op)

    async def por_version(self, producto_id: str, version: str) -> ReglasFallback:
        with operacion("reglas_fallback.consultar") as op:
            reglas = await self._repo.por_version(producto_id, version)
            return await self._encontradas(producto_id, reglas, op)

    async def _encontradas(
        self, producto_id: str, reglas: ReglasFallback | None, op: operacion
    ) -> ReglasFallback:
        """Sin reglas, distingue el producto inexistente de las reglas que faltan."""
        if reglas is not None:
            return reglas
        op.marcar(RESULTADO_RECHAZADO)
        if await self._productos.producto(producto_id) is None:
            raise ProductoNoEncontrado()
        raise ReglasFallbackNoVigentes()
