"""Modelos de la aceptación de la oferta (HU-W15). Puros: sin Pydantic ni E/S.

El sujeto de la aceptación es la cotización (DS2-15): a lo sumo una aceptación por cotización,
sobre su decisión APROBADA y con la oferta vigente según la instantánea de la decisión (DS2-10).
La registra un asesor en nombre del cliente identificado (DS2-09, canal PORTAL_ASESOR) y queda
como evidencia inmutable.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class CanalAceptacion(StrEnum):
    PORTAL_ASESOR = "PORTAL_ASESOR"


class SiguientePasoContratacion(StrEnum):
    FIRMA = "FIRMA"


@dataclass(frozen=True, slots=True)
class Declaraciones:
    """Las declaraciones del tomador (F01). Las dos primeras son obligatorias."""

    leido_condiciones: bool
    informacion_veraz: bool
    debito_automatico: bool
    avisos_electronicos: bool


@dataclass(frozen=True, slots=True)
class Aceptacion:
    id: UUID
    cotizacion_id: UUID
    oferta_id: UUID | None
    decision_id: UUID
    cliente_id: UUID
    # De la instantánea de la decisión: lo que se aceptó.
    huella_calculo: str
    vigente_hasta: datetime
    # DS2-24: versión del texto de las declaraciones que se mostró.
    version_declaraciones: str
    declaraciones: Declaraciones
    canal: CanalAceptacion
    actor_id: str
    aceptada_en: datetime
    idempotency_key: UUID
    huella_solicitud: str
    huella_evidencia: str
    correlation_id: str
