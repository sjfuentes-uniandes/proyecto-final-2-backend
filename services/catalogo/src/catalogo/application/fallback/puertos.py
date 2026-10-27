"""Puertos del módulo de reglas de fallback (HU-W06)."""

from datetime import datetime
from typing import Protocol

from catalogo.domain.fallback.reglas import ReglasFallback


class RepositorioReglasFallback(Protocol):
    async def vigentes(self, producto_id: str, en: datetime) -> ReglasFallback | None:
        """Las reglas del producto con el mayor vigente_desde ≤ en, si no vencieron."""
        ...

    async def por_version(self, producto_id: str, version: str) -> ReglasFallback | None: ...
