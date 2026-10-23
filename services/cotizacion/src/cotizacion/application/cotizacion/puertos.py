"""Puertos de la cotización (HU-W03).

PuertoProductos lanza ProductoNoEncontrado, ReglasNoVigentes y DependenciaNoDisponible.
RepositorioCotizaciones.guardar lanza ConflictoIdempotencia y asigna el código de la oferta. La identidad del cliente y los
perfiles usan los puertos del perfilamiento (PuertoIdentidad y RepositorioPerfiles).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from cotizacion.domain.cotizacion.modelos import Cotizacion, Producto, ReglasPrima


class OrigenDato(StrEnum):
    """De dónde salió el producto o las reglas (paso "Catálogo y reglas" del portal)."""

    CACHE = "CACHE"
    MEMORIA = "MEMORIA"
    CATALOGO = "CATALOGO"


@dataclass(frozen=True, slots=True)
class Resuelto[T]:
    valor: T
    origen: OrigenDato


class PuertoProductos(Protocol):
    async def producto(self, producto_id: str) -> Resuelto[Producto]:
        """El producto tal como está en el catálogo (estado y vigencia incluidos)."""
        ...

    async def reglas_vigentes(self, producto_id: str, en: datetime) -> Resuelto[ReglasPrima]:
        """Las reglas de prima del producto vigentes en el instante dado."""
        ...


class RepositorioCotizaciones(Protocol):
    async def por_idempotencia(self, actor_id: str, clave: UUID) -> Cotizacion | None:
        """Cotización creada antes por el mismo actor con la misma clave de idempotencia."""
        ...

    async def guardar(self, cotizacion: Cotizacion) -> Cotizacion:
        """Guarda la cotización inmutable con su oferta (HU-W04) en una transacción. Devuelve la
        cotización guardada: su oferta ya trae el código legible."""
        ...

    async def por_id(self, cotizacion_id: UUID) -> Cotizacion | None: ...
