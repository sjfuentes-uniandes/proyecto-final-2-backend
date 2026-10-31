"""Puertos de las entregas de avisos (HU-M11)."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from avisos.domain.avisos.modelos import Aviso, Entrega, es_codigo


class Reloj(Protocol):
    def ahora(self) -> datetime: ...


class FalloTransitorio(Exception):
    """El envío falló y puede reintentarse (timeout o 5xx). Lleva solo un código, sin texto libre."""

    def __init__(self, codigo: str) -> None:
        if not es_codigo(codigo):
            raise ValueError("el código de fallo debe ser una constante")
        super().__init__(codigo)
        self.codigo = codigo


class EnviadorPush(Protocol):
    """Envía el aviso a un dispositivo. Devuelve True si el proveedor lo aceptó y False si rechazó
    el token (terminal). Los fallos transitorios lanzan `FalloTransitorio`."""

    async def enviar(self, token: str, aviso: Aviso) -> bool: ...


class RepositorioEntregas[T](Protocol):
    """`T` es la transacción del inbox: las entregas se crean en ella; el resto de operaciones es
    independiente (después del commit)."""

    async def crear_pendientes(self, aviso: Aviso, cliente_id: UUID, ahora: datetime, tx: T) -> int:
        """Una entrega PENDIENTE por cada dispositivo ACTIVO del cliente; las ya existentes (evento,
        dispositivo) no se tocan. Devuelve cuántas creó."""
        ...

    async def reclamar(
        self,
        ahora: datetime,
        hasta: datetime,
        max_intentos: int,
        event_id: UUID | None = None,
    ) -> Entrega | None:
        """Toma, en una sola sentencia atómica, una entrega PENDIENTE vencida o EN_ENVIO con el
        lease vencido, la deja EN_ENVIO hasta `hasta` y suma un intento. None si no hay."""
        ...

    async def marcar_enviada(self, entrega_id: UUID, ahora: datetime) -> None: ...

    async def marcar_token_invalido(self, entrega: Entrega, ahora: datetime, motivo: str) -> None:
        """La entrega pasa a TOKEN_INVALIDO y su dispositivo a INVALIDO, en una transacción."""
        ...

    async def reprogramar(
        self, entrega_id: UUID, proximo_intento: datetime, codigo: str, ahora: datetime
    ) -> None: ...

    async def marcar_fallida(self, entrega_id: UUID, codigo: str, ahora: datetime) -> None: ...

    async def agotar_vencidas(self, ahora: datetime, max_intentos: int) -> int:
        """EN_ENVIO con el lease vencido y sin intentos: pasan a FALLIDO."""
        ...
