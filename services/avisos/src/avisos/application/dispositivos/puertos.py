"""Puertos y errores del registro de dispositivos (HU-M11)."""

from datetime import datetime
from typing import Protocol
from uuid import UUID


class DispositivoNoEncontrado(Exception):
    """No hay dispositivo con ese `dispositivoId`."""


class DispositivoNoAutorizado(Exception):
    """El dispositivo existe pero es de otro cliente."""


class RepositorioDispositivos(Protocol):
    async def registrar(self, cliente_id: UUID, token: str, ahora: datetime) -> UUID:
        """Idempotente por el hash del token. Mismo cliente: no-op o reactiva si estaba INVALIDO.
        Otro cliente: reasigna y falla (FALLIDO) las entregas PENDIENTE/EN_ENVIO del dueño anterior.
        Devuelve el `dispositivoId` (nuevo o existente)."""
        ...

    async def dar_de_baja(self, dispositivo_id: UUID, cliente_id: UUID, ahora: datetime) -> None:
        """Deja el dispositivo INVALIDO (no lo borra) y falla sus entregas PENDIENTE/EN_ENVIO.
        Lanza `DispositivoNoEncontrado` o `DispositivoNoAutorizado` (de otro cliente)."""
        ...
