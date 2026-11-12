"""Coberturas paramétricas, eventos observados y su evaluación (HU-W21, DS3-19 a DS3-22).

Todo es puro: sin E/S, sin reloj y sin SQS. Las cantidades son `Decimal`; los instantes, UTC.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

# Cada tipo de evento se mide en una sola unidad (el contrato de telemetría y `PolizaEmitida`).
UNIDAD_POR_TIPO = {"LLUVIA_ACUMULADA": "mm", "RETRASO_VUELO": "min"}
# La versión de la regla de evaluación de DS3-21: es la del evento `EventoParametricoEvaluado`.
# La de cada cobertura (`coberturaParametrica.versionRegla`) queda en su evaluación.
VERSION_EVALUADOR = "1"


class EstadoCobertura(StrEnum):
    ACTIVA = "ACTIVA"
    CANCELADA = "CANCELADA"


class DecisionPoliza(StrEnum):
    ACTIVA = "ACTIVA"
    NO_ACTIVA = "NO_ACTIVA"


class DecisionEvento(StrEnum):
    ACTIVA = "ACTIVA"
    NO_ACTIVA = "NO_ACTIVA"
    SIN_POLIZAS = "SIN_POLIZAS"


class Razon(StrEnum):
    """Por qué una póliza no activó (en este orden de precedencia, DS3-21) y, a nivel de evento,
    por qué no hubo póliza que evaluar."""

    POLIZA_NO_ACTIVA = "POLIZA_NO_ACTIVA"
    FUERA_DE_VENTANA = "FUERA_DE_VENTANA"
    FUERA_DE_ZONA = "FUERA_DE_ZONA"
    BAJO_UMBRAL = "BAJO_UMBRAL"
    SIN_POLIZAS_EN_ZONA = "SIN_POLIZAS_EN_ZONA"


@dataclass(frozen=True, slots=True)
class Regla:
    """La regla de una póliza paramétrica. La ventana es `[vigente_desde, vigente_hasta)`."""

    tipo_evento: str
    zona: str
    umbral: Decimal
    unidad: str
    valor_pago: Decimal
    moneda: str
    version_regla: str
    vigente_desde: datetime
    vigente_hasta: datetime


@dataclass(frozen=True, slots=True)
class Cobertura:
    """La copia local de la cobertura de una póliza.

    `regla` es None solo en una cancelación que llegó antes que la emisión: la fila existe para
    recordar el orden (una emisión más vieja no la reactiva). `ultimo_evento_en` es el `occurredAt`
    del último evento aplicado y `aggregate_version`, su versión si la traía.
    """

    poliza_id: UUID
    estado: EstadoCobertura
    regla: Regla | None
    ultimo_evento_en: datetime
    aggregate_version: int | None = None
    numero: str | None = None


@dataclass(frozen=True, slots=True)
class EventoObservado:
    """El evento externo ya autenticado y validado, tal como lo publica `adaptador-telemetria`."""

    evento_id: UUID
    fuente: str
    id_externo: str
    evento_origen: str
    tipo_evento: str
    valor_observado: Decimal
    unidad: str
    zona: str
    observado_en: datetime


@dataclass(frozen=True, slots=True)
class Evaluacion:
    """El resultado de una póliza. `valor_pago` y `moneda` solo vienen si activó."""

    poliza_id: UUID
    decision: DecisionPoliza
    razon: Razon | None
    version_regla: str | None
    valor_pago: Decimal | None = None
    moneda: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluacionEvento:
    decision: DecisionEvento
    razon: Razon | None
    evaluaciones: tuple[Evaluacion, ...]

    @property
    def evaluadas(self) -> int:
        return len(self.evaluaciones)

    @property
    def activaciones(self) -> int:
        return sum(1 for e in self.evaluaciones if e.decision is DecisionPoliza.ACTIVA)
