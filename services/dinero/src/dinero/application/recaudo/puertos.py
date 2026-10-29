"""Puertos del recaudo (HU-W16). El consumidor de `OfertaAceptada` (16.10) y la conciliación (16.11)
los usan; las pruebas inyectan dobles.

Las operaciones del repositorio aceptan una transacción (`tx`) que ya abrió otro: la del inbox, para
que el movimiento y la fila del inbox se confirmen juntos. Sin `tx`, cada operación abre la suya.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from dinero.domain.recaudo.modelos import Recaudo


class Reloj(Protocol):
    def ahora(self) -> datetime:
        """Instante actual en UTC."""
        ...


@dataclass(frozen=True, slots=True)
class Creacion:
    """Resultado de `crear_pendiente`: el movimiento guardado y si lo creó esta llamada."""

    recaudo: Recaudo
    nuevo: bool


class RecaudosRepositorio[T](Protocol):
    async def crear_pendiente(self, recaudo: Recaudo, tx: T | None = None) -> Creacion:
        """Inserta el movimiento PENDIENTE. Si ya hay uno con su `claveRecaudo`, no inserta nada y
        devuelve el existente con `nuevo = False`."""
        ...

    async def por_clave(self, clave_recaudo: UUID, tx: T | None = None) -> Recaudo | None: ...

    async def registrar_resultado(self, recaudo: Recaudo, tx: T | None = None) -> int:
        """Escribe el estado y el resultado de `recaudo` solo si la fila sigue PENDIENTE. Devuelve
        las filas cambiadas: 0 si otra tarea ya lo resolvió."""
        ...
