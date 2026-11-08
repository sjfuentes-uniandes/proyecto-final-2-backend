"""Registrar un lote recibido como un objeto encadenado (HU-W24, D3).

En una transacción: toma la cabeza de la cadena con bloqueo (las N tareas de `auditoria` escriben
en serie), registra los `eventId` en el inbox (los que ya estaban son duplicados), sella los nuevos
como un lote, escribe el objeto y avanza la cabeza. Si algo falla, rollback: ningún `eventId` queda
en el inbox y el lote vuelve a SQS entero.

El bloqueo va primero: dos tareas con `eventId` comunes no se cruzan en el inbox (sin interbloqueo)
y la segunda ve los de la primera como duplicados.

Si el objeto se escribió pero el commit falla, ese objeto queda huérfano: su `previousHash` enlaza
con la cabeza confirmada, pero ninguna cabeza apunta a él. El reintento escribe otro objeto con la
misma secuencia y otro `hash` (la clave del objeto lleva los dos). `verificar_cadena` recorre desde
la cabeza confirmada; los huérfanos no forman parte de la cadena.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from auditoria.domain.registro import EventoAuditable, Lote, sellar


class TransaccionCadena(Protocol):
    async def cabeza(self) -> tuple[int, str]:
        """(secuencia, hash) de la cabeza, bloqueada hasta el fin de la transacción."""
        ...

    async def registrar(
        self, eventos: Sequence[EventoAuditable], recibido_en: datetime
    ) -> set[str]:
        """Inserta los `eventId` en el inbox; devuelve los nuevos."""
        ...

    async def avanzar(self, secuencia: int, hash_: str) -> None: ...


class Cadena(Protocol):
    def transaccion(self) -> AbstractAsyncContextManager[TransaccionCadena]: ...


class Almacen(Protocol):
    async def guardar(self, lote: Lote) -> None: ...


class Reloj(Protocol):
    def ahora(self) -> datetime: ...


@dataclass(frozen=True)
class Resultado:
    nuevos: int
    duplicados: int
    secuencia: int | None


class RegistrarLote:
    def __init__(self, cadena: Cadena, almacen: Almacen, reloj: Reloj) -> None:
        self._cadena = cadena
        self._almacen = almacen
        self._reloj = reloj

    async def __call__(self, eventos: Sequence[EventoAuditable]) -> Resultado:
        # El mismo eventId dos veces en el lote cuenta una vez (la segunda es un duplicado).
        unicos = list({evento.event_id: evento for evento in eventos}.values())
        if not unicos:
            return Resultado(0, 0, None)
        ahora = self._reloj.ahora()
        async with self._cadena.transaccion() as tx:
            secuencia, cabeza = await tx.cabeza()
            nuevos_ids = await tx.registrar(unicos, ahora)
            nuevos = [evento for evento in unicos if evento.event_id in nuevos_ids]
            duplicados = len(eventos) - len(nuevos)
            if not nuevos:
                return Resultado(0, duplicados, None)
            lote = sellar(secuencia + 1, cabeza, nuevos, ahora.isoformat())
            await self._almacen.guardar(lote)
            await tx.avanzar(lote.secuencia, lote.hash)
        return Resultado(len(nuevos), duplicados, lote.secuencia)
