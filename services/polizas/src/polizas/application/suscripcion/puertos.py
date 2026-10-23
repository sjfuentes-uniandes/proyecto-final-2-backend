"""Puertos de la suscripción (HU-W13, W13-B15)."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from polizas.domain.suscripcion.modelos import (
    Actor,
    CasoBandeja,
    Decision,
    FiltrosBandeja,
    InsumosSuscripcion,
    ReglasSuscripcion,
)


class InsumosOfertaPuerto(Protocol):
    async def insumos(self, cotizacion_id: UUID, actor: Actor) -> InsumosSuscripcion:
        """Los insumos de la oferta. Lanza CotizacionNoEncontrada o DependenciaNoDisponible."""
        ...


class ReglasSuscripcionPuerto(Protocol):
    async def vigentes(self, producto_id: str) -> ReglasSuscripcion:
        """Las reglas vigentes ahora. Lanza ReglasNoVigentes o DependenciaNoDisponible."""
        ...

    async def por_version(self, producto_id: str, version: str) -> ReglasSuscripcion:
        """Una versión publicada (no cambia). Lanza ReglasNoVigentes o DependenciaNoDisponible."""
        ...


class DecisionesRepositorio(Protocol):
    async def guardar(self, decision: Decision) -> bool:
        """Inserta la decisión y su evento en una transacción. False si ya existía una decisión
        de la cotización o de la clave (ganó otra solicitud): no inserta nada."""
        ...

    async def por_id(self, decision_id: UUID) -> Decision | None: ...

    async def por_cotizacion(self, cotizacion_id: UUID) -> Decision | None: ...

    async def por_idempotencia(self, clave: UUID) -> Decision | None: ...

    async def bandeja(
        self, filtros: FiltrosBandeja, limite: int, despues: tuple[datetime, UUID] | None
    ) -> list[CasoBandeja]:
        """Hasta `limite` casos en revisión posteriores a `despues`, del más antiguo al más
        reciente (por decidida_en e id)."""
        ...

    async def contar_bandeja(self, filtros: FiltrosBandeja) -> int: ...

    async def contar_dia(self, desde: datetime, hasta: datetime) -> tuple[int, int, float | None]:
        """Decisiones automáticas resueltas (APROBADA o RECHAZADA), en revisión y duración media
        (ms) en [desde, hasta)."""
        ...

    async def contar_fuera_del_compromiso(self, antes_de: datetime) -> int:
        """Casos en revisión decididos antes de `antes_de`."""
        ...


class GeneradorCodigos(Protocol):
    async def siguiente(self, anio: int) -> str:
        """Código legible único DEC-AAAA-NNNNNN."""
        ...


class Reloj(Protocol):
    def ahora(self) -> datetime:
        """Instante actual en UTC."""
        ...
