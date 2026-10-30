from typing import Protocol
from uuid import UUID

from bff_movil.domain.polizas.modelos import Poliza


class ConsultaPolizas(Protocol):
    async def listar(self, cliente_id: UUID, actor_id: str) -> list[Poliza]:
        """Pólizas del cliente, vencidas incluidas; lanza ServicioNoDisponible."""
        ...

    async def obtener(self, cliente_id: UUID, poliza_id: UUID, actor_id: str) -> Poliza:
        """Lanza PolizaNoEncontrada, PolizaNoAutorizada o ServicioNoDisponible."""
        ...
