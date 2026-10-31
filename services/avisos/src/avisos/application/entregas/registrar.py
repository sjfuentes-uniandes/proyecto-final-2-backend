"""Caso de uso: crear las entregas de un aviso dentro de la transacción del inbox (HU-M11)."""

from uuid import UUID

from avisos.application.entregas.puertos import Reloj, RepositorioEntregas
from avisos.domain.avisos.modelos import aviso_de_poliza_emitida


class RegistrarEntregas[T]:
    def __init__(self, entregas: RepositorioEntregas[T], reloj: Reloj) -> None:
        self._entregas = entregas
        self._reloj = reloj

    async def poliza_emitida(self, event_id: UUID, poliza_id: UUID, cliente_id: UUID, tx: T) -> int:
        """Crea una entrega PENDIENTE por dispositivo activo del cliente; devuelve cuántas."""
        aviso = aviso_de_poliza_emitida(event_id, poliza_id)
        return await self._entregas.crear_pendientes(aviso, cliente_id, self._reloj.ahora(), tx)
