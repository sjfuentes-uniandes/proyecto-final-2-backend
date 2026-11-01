"""Puerto de lectura del estado y el listado de pólizas (HU-W18)."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from consulta.domain.polizas.estado import EstadoVigencia
from consulta.domain.polizas.modelos import PolizaProyectada


@dataclass(frozen=True, slots=True)
class FiltroPolizas:
    """`hoy` y `dias_por_vencer` permiten evaluar el filtro de estado dentro de la consulta."""

    hoy: date
    dias_por_vencer: int
    numero: str | None = None
    ramo: str | None = None
    estado: EstadoVigencia | None = None
    vigencia_desde: date | None = None


@dataclass(frozen=True, slots=True)
class ResultadoBusqueda:
    polizas: tuple[PolizaProyectada, ...]
    total: int
    actualizado_en: datetime | None


class LecturaEstadoPolizas(Protocol):
    async def por_id(self, poliza_id: UUID) -> PolizaProyectada | None: ...

    async def buscar(self, filtro: FiltroPolizas, pagina: int, tamano: int) -> ResultadoBusqueda:
        """Por `emitida_en` descendente y `poliza_id`. `actualizado_en` es el mayor
        `proyectada_en` de toda la proyección, sin filtro."""
        ...
