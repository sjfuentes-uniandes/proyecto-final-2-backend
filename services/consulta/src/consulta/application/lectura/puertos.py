"""Puertos de la lectura de pólizas del cliente (HU-M10)."""

from typing import Protocol
from uuid import UUID

from consulta.domain.polizas.modelos import PolizaProyectada


class PolizaNoEncontrada(Exception):
    """No hay póliza con ese `polizaId`."""


class PolizaNoAutorizada(Exception):
    """La póliza existe pero es de otro cliente."""


class LecturaPolizas(Protocol):
    async def por_id(self, poliza_id: UUID) -> PolizaProyectada | None: ...

    async def por_cliente(self, cliente_id: UUID) -> list[PolizaProyectada]:
        """Solo las del cliente, por `vigencia_hasta` descendente."""
        ...
