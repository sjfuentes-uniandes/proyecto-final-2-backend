"""Puertos del módulo de reglas de suscripción (HU-W13)."""

from datetime import datetime
from typing import Protocol

from catalogo.domain.suscripcion.reglas import ReglasSuscripcion


class RepositorioReglasSuscripcion(Protocol):
    async def vigentes(self, producto_id: str, en: datetime) -> ReglasSuscripcion | None:
        """Las reglas del producto con el mayor vigente_desde ≤ en."""
        ...

    async def por_version(self, producto_id: str, version: str) -> ReglasSuscripcion | None: ...
