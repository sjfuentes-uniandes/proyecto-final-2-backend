from typing import Protocol
from uuid import UUID


class GestionDispositivos(Protocol):
    async def registrar(self, cliente_id: UUID, token: str, actor_id: str) -> UUID:
        """Devuelve el dispositivoId; lanza ServicioNoDisponible."""
        ...

    async def dar_de_baja(self, cliente_id: UUID, dispositivo_id: UUID, actor_id: str) -> None:
        """Lanza DispositivoNoEncontrado, DispositivoNoAutorizado o ServicioNoDisponible."""
        ...
