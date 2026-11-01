"""Puertos de la proyección de pólizas (HU-M10)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada


@dataclass(frozen=True, slots=True)
class ProductoCatalogo:
    """Lo que la proyección copia del producto vigente del catálogo."""

    id: str
    version: str
    nombre: str
    ramo: str
    coberturas: tuple[CoberturaProyectada, ...]


class DependenciaNoDisponible(Exception):
    """El catálogo no respondió o respondió algo inválido: el mensaje se reintenta."""

    def __init__(self, dependencia: str) -> None:
        super().__init__(dependencia)
        self.dependencia = dependencia


class VersionProductoDistinta(Exception):
    """La versión vigente del catálogo no es la del evento: el mensaje se reintenta o va a la DLQ."""

    def __init__(self, producto_id: str, esperada: str, vigente: str) -> None:
        super().__init__(producto_id)
        self.producto_id = producto_id
        self.esperada = esperada
        self.vigente = vigente


class ConsultaCatalogo(Protocol):
    async def producto(self, producto_id: str) -> ProductoCatalogo: ...


class ProyeccionPolizas[T](Protocol):
    async def insertar(self, poliza: PolizaProyectada, tx: T) -> bool:
        """Inserta la póliza; False si el `polizaId` ya existía (no se modifica)."""
        ...


class Reloj(Protocol):
    def ahora(self) -> datetime: ...
