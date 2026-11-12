"""Puertos de la recepción de eventos externos (HU-W21)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from adaptador_telemetria.domain.telemetria.modelos import EventoCanonico


class DependenciaNoDisponible(Exception):
    """El archivo o la base no respondieron: nada quedó publicado y la fuente puede reenviar.

    Solo lleva el nombre de la dependencia, nunca el mensaje de la causa (puede traer datos).
    """

    def __init__(self, dependencia: str) -> None:
        super().__init__(dependencia)
        self.dependencia = dependencia


@dataclass(frozen=True)
class Registrado:
    """`nuevo` es False si el evento ya estaba: `recibido_en` es el del primer envío."""

    nuevo: bool
    recibido_en: datetime


class Archivo(Protocol):
    async def guardar(self, clave: str, cuerpo: bytes, metadatos: Mapping[str, str]) -> None:
        """Guarda el original tal cual llegó. Idempotente: la clave es determinista."""
        ...


class Registro(Protocol):
    async def recibido(self, evento_id: UUID) -> datetime | None:
        """`recibido_en` del evento si ya se recibió; una lectura corta."""
        ...

    async def registrar(
        self, evento: EventoCanonico, recibido_en: datetime, archivo_ref: str, correlation_id: str
    ) -> Registrado:
        """En una transacción: el registro del evento y, si es nuevo, su fila en el Outbox."""
        ...


class Reloj(Protocol):
    def ahora(self) -> datetime: ...
