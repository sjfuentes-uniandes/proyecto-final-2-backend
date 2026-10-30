from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


class PolizaNoAutorizada(Exception):
    """La póliza es de otro cliente. Marcadora: no lleva datos de la póliza."""


class PolizaNoEncontrada(Exception):
    """La póliza no existe. Marcadora."""


@dataclass(frozen=True)
class Vigencia:
    desde: date
    hasta: date


@dataclass(frozen=True)
class Cobertura:
    codigo: str
    nombre: str
    obligatoria: bool


@dataclass(frozen=True)
class Poliza:
    poliza_id: UUID
    numero: str
    estado: str
    producto_nombre: str
    ramo: str
    vigencia: Vigencia
    vigente: bool
    prima: Decimal
    suma_asegurada: Decimal
    moneda: str
    periodicidad: str
    coberturas: tuple[Cobertura, ...]
    documento_huella: str
    firma_id: str
    emitida_en: datetime
