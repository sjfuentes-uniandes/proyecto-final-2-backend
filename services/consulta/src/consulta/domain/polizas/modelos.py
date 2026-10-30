"""Póliza proyectada para las lecturas del cliente (HU-M10). Sin datos personales."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

# Colombia no tiene horario de verano desde 1993: un desfase fijo es America/Bogota.
BOGOTA = timezone(timedelta(hours=-5), "America/Bogota")


@dataclass(frozen=True, slots=True)
class Vigencia:
    """Fechas inclusivas en America/Bogota."""

    desde: date
    hasta: date


@dataclass(frozen=True, slots=True)
class CoberturaProyectada:
    codigo: str
    nombre: str
    obligatoria: bool


@dataclass(frozen=True, slots=True)
class PolizaProyectada:
    poliza_id: UUID
    cliente_id: UUID
    numero: str
    estado: str
    producto_id: str
    producto_version: str
    producto_nombre: str
    ramo: str
    vigencia: Vigencia
    prima: Decimal
    suma_asegurada: Decimal
    moneda: str
    periodicidad: str
    coberturas: tuple[CoberturaProyectada, ...]
    documento_huella: str
    firma_id: str
    emitida_en: datetime
    proyectada_en: datetime


def es_vigente(vigencia: Vigencia, hoy: date) -> bool:
    return vigencia.desde <= hoy <= vigencia.hasta


def hoy_en_bogota(ahora: datetime) -> date:
    """La fecha de Bogotá de un instante con zona."""
    return ahora.astimezone(BOGOTA).date()
