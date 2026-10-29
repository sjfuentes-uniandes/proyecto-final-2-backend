"""Listado de las cotizaciones con oferta ("Cotizaciones abiertas" del portal).

Solo lectura sobre lo que la cotización ya guarda (HU-W03, HU-W04, HU-W06): ningún dato nuevo ni
personal. El estado de la oferta se deriva del instante de la consulta, como en la lectura por id.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from cotizacion.domain.cotizacion.modelos import EstadoOferta
from cotizacion.domain.perfilamiento.modelos import NivelRiesgo


@dataclass(frozen=True, slots=True)
class FiltrosCotizaciones:
    """`q` busca por el inicio del código (COT-AAAA-NNNNN, con o sin `COT-`). `socio_id` lo fija el
    caso de uso para un SOCIO (solo ve las suyas); un ASESOR ve todas."""

    estado: EstadoOferta | None = None
    q: str | None = None
    socio_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResumenCotizacion:
    cotizacion_id: UUID
    codigo: str
    cliente_id: UUID
    socio_id: str | None
    producto_id: str
    version_producto: str
    moneda: str
    periodicidad: str
    suma_asegurada: Decimal
    prima: Decimal
    nivel_riesgo: NivelRiesgo | None
    degradada: bool
    creada_en: datetime
    vigente_hasta: datetime

    def estado(self, ahora: datetime) -> EstadoOferta:
        return EstadoOferta.VIGENTE if ahora < self.vigente_hasta else EstadoOferta.VENCIDA


@dataclass(frozen=True, slots=True)
class PaginaCotizaciones:
    items: tuple[ResumenCotizacion, ...]
    siguiente_cursor: str | None
    total: int
