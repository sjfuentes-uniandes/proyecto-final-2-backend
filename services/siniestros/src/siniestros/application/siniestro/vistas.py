"""Lo que devuelven los casos de uso de siniestros (HU-W19). Datos ya calculados con el dominio;
el adaptador HTTP los convierte en DTO. Nunca llevan la clave del objeto ni una URL, salvo
`EnlaceEmitido`, que es la respuesta de un solo uso."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from siniestros.domain.siniestro.cobertura import SoportePendiente, Verificacion
from siniestros.domain.siniestro.coherencia import Coherencia
from siniestros.domain.siniestro.modelos import (
    EntradaHistorial,
    EstadoSiniestro,
    Evidencia,
    IntegridadEvidencia,
    OrigenSiniestro,
    PrioridadSiniestro,
    Siniestro,
)
from siniestros.domain.siniestro.plazos import prioridad, tiempo_restante
from siniestros.domain.siniestro.tramite import PasoDeTramite

ROL_ANALISTA = "ANALISTA_SINIESTROS"


class TipoBandeja(StrEnum):
    MIA = "MIA"
    SIN_ASIGNAR = "SIN_ASIGNAR"


class MotivoDenegacion(StrEnum):
    SIN_GRUPO = "SIN_GRUPO"
    ROL_AUSENTE = "ROL_AUSENTE"
    ROL_INVALIDO = "ROL_INVALIDO"


class RecursoSiniestro(StrEnum):
    BANDEJA = "BANDEJA"
    RESUMEN = "RESUMEN"
    SIGUIENTE = "SIGUIENTE"
    SINIESTRO = "SINIESTRO"
    EVIDENCIA = "EVIDENCIA"


class OrigenDenegacion(StrEnum):
    BFF_WEB = "BFF_WEB"
    SINIESTROS = "SINIESTROS"


@dataclass(frozen=True)
class Actor:
    """Quien hace la solicitud, tal como lo entrega `bff-web` (DS3-10)."""

    id: str
    rol: str | None = None
    nombre: str | None = None


@dataclass(frozen=True)
class Plazos:
    """Compromiso y umbrales de prioridad (DS3-04), de la configuración."""

    compromiso_horas: int
    alta_min: int
    media_min: int


@dataclass(frozen=True)
class ResumenCaso:
    siniestro: Siniestro
    tiempo_restante_min: int
    prioridad: PrioridadSiniestro


def resumen_de(siniestro: Siniestro, ahora: datetime, plazos: Plazos) -> ResumenCaso:
    restante = tiempo_restante(siniestro, ahora, compromiso_horas=plazos.compromiso_horas)
    return ResumenCaso(
        siniestro,
        restante,
        prioridad(restante, alta_min=plazos.alta_min, media_min=plazos.media_min),
    )


@dataclass(frozen=True)
class FiltroBandeja:
    bandeja: TipoBandeja
    actor_id: UUID
    corte: datetime
    plazos: Plazos
    estado: EstadoSiniestro | None = None
    origen: OrigenSiniestro | None = None
    prioridad: PrioridadSiniestro | None = None
    texto: str | None = None
    limite: int = 20
    cursor: str | None = None


@dataclass(frozen=True)
class PaginaSiniestros:
    items: tuple[Siniestro, ...]
    total: int
    siguiente_cursor: str | None


@dataclass(frozen=True)
class PaginaBandeja:
    items: tuple[ResumenCaso, ...]
    total: int
    siguiente_cursor: str | None


@dataclass(frozen=True)
class Indicadores:
    pendientes: int
    vencen_hoy: int
    fuera_de_compromiso: int
    resueltos_semana: int
    resueltos_semana_previa: int
    tiempo_medio_decision_horas: float | None
    compromiso_horas: int
    asignados_al_actor: int
    sin_asignar: int


@dataclass(frozen=True)
class DetalleLeido:
    """Lo que lee el repositorio para armar el detalle, en una sola transacción corta."""

    siniestro: Siniestro
    evidencias: tuple[Evidencia, ...]
    historial: tuple[EntradaHistorial, ...]


@dataclass(frozen=True)
class EvidenciaVerificada:
    evidencia: Evidencia
    integridad: IntegridadEvidencia


@dataclass(frozen=True)
class DetalleCaso:
    resumen: ResumenCaso
    verificacion_cobertura: tuple[Verificacion, ...]
    coherencia: Coherencia
    evidencias: tuple[EvidenciaVerificada, ...]
    soportes_pendientes: tuple[SoportePendiente, ...]
    tramite: tuple[PasoDeTramite, ...]
    historial: tuple[EntradaHistorial, ...]


@dataclass(frozen=True)
class EnlaceEmitido:
    url: str
    expira_en: datetime
    tipo_contenido: str


@dataclass(frozen=True)
class AccesoDenegado:
    actor_id: str
    rol: str | None
    recurso: RecursoSiniestro
    recurso_id: str | None
    motivo: MotivoDenegacion
    origen: OrigenDenegacion
