"""La oferta que el socio presenta al cliente (HU-W04). La calcula y la guarda `cotizacion`.

api-socios no decide el precio ni la vigencia: recibe la oferta con su estado al instante de la
consulta (el reloj es el de `cotizacion`) y aplica la regla del canal: al socio nunca se le entrega
una oferta vencida para presentarla, se le pide cotizar de nuevo (AC3).
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID


class EstadoOferta(StrEnum):
    VIGENTE = "VIGENTE"
    VENCIDA = "VENCIDA"


@dataclass(frozen=True, slots=True)
class SolicitudOferta:
    """Lo que el socio envía para cotizar; nunca un perfil ni otro socio."""

    cliente_id: UUID
    producto_id: str
    fecha_nacimiento: date
    plazo_credito_meses: int
    suma_asegurada: int
    coberturas_opcionales: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CoberturaOferta:
    codigo: str
    nombre: str
    suma_asegurada: Decimal


@dataclass(frozen=True, slots=True)
class ExclusionResumida:
    codigo: str
    descripcion: str


@dataclass(frozen=True, slots=True)
class Oferta:
    id: UUID
    codigo: str
    cotizacion_id: UUID
    producto_id: str
    precio: Decimal
    moneda: str
    periodicidad: str
    coberturas: tuple[CoberturaOferta, ...]
    exclusiones: tuple[ExclusionResumida, ...]
    version: int
    version_reglas: str
    creada_en: datetime
    vigente_hasta: datetime
    estado: EstadoOferta


class OfertaNoEncontrada(Exception):
    """No existe, es de otro socio o es anterior a HU-W04: no se distingue."""


class OfertaVencida(Exception):
    def __init__(self, oferta: Oferta) -> None:
        super().__init__(str(oferta.id))
        self.oferta = oferta


class CotizacionRechazada(Exception):
    """`cotizacion` rechazó la solicitud con un código estable (HU-W03) que el socio recibe igual."""

    def __init__(self, codigo: str, estado_http: int, detalles: dict[str, Any]) -> None:
        super().__init__(codigo)
        self.codigo = codigo
        self.estado_http = estado_http
        self.detalles = detalles


class CotizacionNoDisponible(Exception):
    """`cotizacion` no respondió a tiempo, falló o respondió fuera de su contrato."""


def asegurar_presentable(oferta: Oferta) -> Oferta:
    """Una oferta vencida no se entrega al socio: debe cotizar de nuevo (AC3)."""
    if oferta.estado is EstadoOferta.VENCIDA:
        raise OfertaVencida(oferta)
    return oferta
