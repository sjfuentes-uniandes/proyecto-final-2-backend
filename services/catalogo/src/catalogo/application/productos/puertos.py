"""Puertos del módulo de productos."""

from datetime import datetime
from typing import Protocol

from catalogo.domain.productos.producto import Producto, ReglasPrima


class RepositorioProductos(Protocol):
    async def producto(self, producto_id: str) -> Producto | None: ...

    async def reglas_vigentes(self, producto_id: str, en: datetime) -> ReglasPrima | None:
        """Las reglas del producto con el mayor vigente_desde ≤ en."""
        ...

    async def reglas_por_version(self, producto_id: str, version: str) -> ReglasPrima | None: ...
