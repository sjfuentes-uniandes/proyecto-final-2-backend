"""Modelo canónico del adaptador de datos (02, CS-06). Puro: sin E/S."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

TipoFuente = Literal["FINANZAS_ABIERTAS", "DATOS_ABIERTOS"]
EstadoValor = Literal["DISPONIBLE", "NO_DISPONIBLE"]


class Fuente(StrEnum):
    """Copia propia de la de cotizacion: son servicios distintos."""

    FA_PRODUCTOS_VIGENTES = "FA_PRODUCTOS_VIGENTES"
    FA_HISTORIAL_PAGOS_12M = "FA_HISTORIAL_PAGOS_12M"
    FA_INGRESOS_AGREGADOS = "FA_INGRESOS_AGREGADOS"
    DA_REGISTROS_PUBLICOS = "DA_REGISTROS_PUBLICOS"


class Aliado(StrEnum):
    OPEN_FINANCE = "OPEN_FINANCE"
    DATOS_ABIERTOS = "DATOS_ABIERTOS"


@dataclass(frozen=True, slots=True)
class ValorCanonico:
    estado: EstadoValor
    valor: Decimal | None


@dataclass(frozen=True, slots=True)
class RespuestaConsulta:
    fuente: Fuente
    tipo: TipoFuente
    proveedor: str
    version_fuente: str
    confianza: Decimal
    consultada_en: datetime
    campos: Mapping[str, ValorCanonico]
    campos_descartados: int


class RespuestaInvalida(Exception):
    """La respuesta del proveedor no cumple el formato esperado."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo
