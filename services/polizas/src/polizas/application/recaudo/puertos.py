"""Puertos del estado del recaudo (HU-W16, 16.13). La aceptación y la decisión usan los suyos."""

from typing import Protocol
from uuid import UUID

from polizas.domain.recaudo.modelos import ResultadoRecaudo


class RecaudosRepositorio(Protocol):
    async def por_clave(self, clave_recaudo: UUID) -> ResultadoRecaudo | None: ...


class ProyeccionRecaudos[T](Protocol):
    """Escribe la proyección en la transacción `T` del inbox (modo a de `solventa_common.inbox`)."""

    async def guardar(self, resultado: ResultadoRecaudo, tx: T) -> ResultadoRecaudo | None:
        """Guarda el resultado si la clave aún no tiene estado final y devuelve None. Si ya tiene
        uno, no cambia nada y devuelve el que estaba."""
        ...

    async def aceptacion_conocida(self, clave_recaudo: UUID, tx: T) -> bool:
        """True si existe la aceptación cuyo id es la clave."""
        ...
