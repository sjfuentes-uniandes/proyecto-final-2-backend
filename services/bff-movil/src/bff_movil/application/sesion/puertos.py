from typing import Protocol
from uuid import UUID

from bff_movil.domain.sesion.modelos import EstadoIdentidad


class ResolutorCliente(Protocol):
    async def resolver(self, sub: str) -> UUID:
        """clienteId del titular del token; lanza ClienteNoEncontrado si no hay relación."""
        ...


class ConsultaIdentidad(Protocol):
    async def obtener(self, cliente_id: UUID, actor_id: str) -> EstadoIdentidad:
        """Estado de identidad; lanza ClienteNoEncontrado o ServicioNoDisponible."""
        ...
