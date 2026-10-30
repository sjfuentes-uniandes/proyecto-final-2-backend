"""Puertos de la póliza (HU-W17, 17.6). Todo corre en una transacción `T` recibida: la del inbox
para crearla junto con la proyección del recaudo; la de la tarea emisora (17.8) para el resto."""

from typing import Protocol
from uuid import UUID

from polizas.domain.poliza.modelos import OrigenPoliza, Poliza


class PolizasRepositorio[T](Protocol):
    async def origen(self, aceptacion_id: UUID, tx: T) -> OrigenPoliza | None:
        """Lo que la póliza toma de la aceptación y de su decisión; None si polizas no conoce la
        aceptación."""
        ...

    async def siguiente_secuencia(self, tx: T) -> int:
        """El siguiente valor de la secuencia de números. No vuelve atrás con un rollback."""
        ...

    async def crear(self, poliza: Poliza, tx: T) -> bool:
        """Inserta la póliza; False si la aceptación ya tenía una (no cambia nada)."""
        ...

    async def por_aceptacion(self, aceptacion_id: UUID, tx: T) -> Poliza | None: ...

    async def actualizar(self, poliza: Poliza, tx: T) -> bool:
        """Guarda una transición si la póliza sigue EMITIENDO; False si ya no (otra tarea la
        activó)."""
        ...
