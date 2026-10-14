"""Puertos del módulo de reglas."""

from datetime import datetime
from typing import Protocol

from catalogo.domain.reglas.modelo import ModeloPublicado


class RepositorioModelos(Protocol):
    async def guardar(self, modelo: ModeloPublicado) -> None:
        """Inserta el modelo; lanza VersionExistente si la versión ya existe."""
        ...

    async def vigente(self, producto_id: str, en: datetime) -> ModeloPublicado | None:
        """El modelo del producto con el mayor vigente_desde ≤ en."""
        ...

    async def por_version(self, version: str) -> ModeloPublicado | None: ...


class Reloj(Protocol):
    def ahora(self) -> datetime:
        """Instante actual en UTC."""
        ...
