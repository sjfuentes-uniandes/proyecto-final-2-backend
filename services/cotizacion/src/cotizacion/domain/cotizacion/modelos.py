"""Modelos de la cotización (HU-W03). Puros: sin Pydantic ni E/S; los montos son Decimal.

Según el modelo de dominio (docs/arquitectura/modelo_dominio.puml): la cotización la recibe un
cliente (1), la solicita a lo sumo un socio (0..1), ofrece un producto (1), utiliza a lo sumo un
perfil de riesgo (0..1) y el producto se configura con reglas versionadas.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from cotizacion.domain.perfilamiento.modelos import NivelRiesgo

# Factor de las reglas cuando la cotización no utiliza un perfil con nivel (0..1 del dominio).
SIN_PERFIL = "SIN_PERFIL"


class EstadoProducto(StrEnum):
    ACTIVO = "ACTIVO"
    INACTIVO = "INACTIVO"


class EstadoCotizacion(StrEnum):
    CALCULADA = "CALCULADA"


class RolActor(StrEnum):
    ASESOR = "ASESOR"
    SOCIO = "SOCIO"


@dataclass(frozen=True, slots=True)
class Actor:
    id: str
    rol: RolActor


@dataclass(frozen=True, slots=True)
class Cobertura:
    codigo: str
    nombre: str
    obligatoria: bool


@dataclass(frozen=True, slots=True)
class Producto:
    id: str
    version: str
    nombre: str
    ramo: str
    pais: str
    moneda: str
    estado: EstadoProducto
    vigente_desde: datetime
    # Vigencia semiabierta [vigente_desde, vigente_hasta); None = sin fin.
    vigente_hasta: datetime | None
    coberturas: tuple[Cobertura, ...]

    @property
    def opcionales(self) -> frozenset[str]:
        return frozenset(c.codigo for c in self.coberturas if not c.obligatoria)


@dataclass(frozen=True, slots=True)
class Tramo:
    """Factor para los valores ≤ `hasta`; None = tramo abierto (el último)."""

    hasta: Decimal | None
    factor: Decimal


@dataclass(frozen=True, slots=True)
class FactorVariable:
    codigo: str
    variable: str
    tramos: tuple[Tramo, ...]


@dataclass(frozen=True, slots=True)
class Rango:
    minimo: Decimal
    maximo: Decimal

    def contiene(self, valor: Decimal) -> bool:
        return self.minimo <= valor <= self.maximo


@dataclass(frozen=True, slots=True)
class Limites:
    suma_asegurada: Rango
    plazo_credito_meses: Rango
    edad: Rango


@dataclass(frozen=True, slots=True)
class ReglasPrima:
    producto_id: str
    version: str
    hash: str
    vigente_desde: datetime
    moneda: str
    periodicidad: str
    tasa_base: Decimal
    factores_nivel: Mapping[str, Decimal]
    factores_variables: tuple[FactorVariable, ...]
    # Cobertura opcional → tasa del recargo sobre la prima ajustada.
    recargos: Mapping[str, Decimal]
    prima_minima: Decimal
    prima_maxima: Decimal
    redondeo_escala: int
    redondeo_modo: str
    limites: Limites


@dataclass(frozen=True, slots=True)
class EntradasPrima:
    """Lo mínimo que se conserva de la solicitud: sin fecha de nacimiento ni documento."""

    suma_asegurada: Decimal
    plazo_credito_meses: int
    edad: int
    nivel_riesgo: NivelRiesgo | None
    coberturas_opcionales: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FactorAplicado:
    codigo: str
    valor: str
    factor: Decimal


@dataclass(frozen=True, slots=True)
class RecargoAplicado:
    cobertura: str
    tasa: Decimal
    valor: Decimal


@dataclass(frozen=True, slots=True)
class Desglose:
    """Cada monto ya redondeado; `subtotal` y `prima_total` son sumas exactas de las líneas."""

    prima_base: Decimal
    factores: tuple[FactorAplicado, ...]
    prima_ajustada: Decimal
    recargos: tuple[RecargoAplicado, ...]
    subtotal: Decimal
    ajuste_limite: Decimal
    prima_total: Decimal


@dataclass(frozen=True, slots=True)
class Cotizacion:
    id: UUID
    cliente_id: UUID
    socio_id: str | None
    producto_id: str
    version_producto: str
    moneda: str
    periodicidad: str
    version_reglas: str
    hash_reglas: str
    perfil_id: UUID | None
    entradas: EntradasPrima
    coberturas: tuple[Cobertura, ...]
    desglose: Desglose
    huella_calculo: str
    estado: EstadoCotizacion
    idempotency_key: UUID
    huella_solicitud: str
    calculado_en: datetime
    duracion_ms: int
    correlation_id: str
    actor: Actor
