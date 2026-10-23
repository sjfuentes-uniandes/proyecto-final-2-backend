"""Modelos de la suscripción automática (HU-W13). Puros: sin Pydantic ni E/S; montos en Decimal.

Según el modelo de dominio, la decisión de suscripción es de Pólizas: una cotización produce a lo
sumo una decisión (DS2-05, DS2-15), que conserva sus entradas, sus razones y la versión de las
reglas con que se tomó (100 % reconstruible).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class ResultadoDecision(StrEnum):
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"
    REVISION_REQUERIDA = "REVISION_REQUERIDA"


class TipoDecision(StrEnum):
    AUTOMATICA = "AUTOMATICA"


class TipoRegla(StrEnum):
    EXCLUYENTE = "EXCLUYENTE"
    REVISION = "REVISION"


class ResultadoRegla(StrEnum):
    CUMPLE = "CUMPLE"
    NO_CUMPLE = "NO_CUMPLE"
    NO_EVALUABLE = "NO_EVALUABLE"


class Visibilidad(StrEnum):
    EXPLICABLE = "EXPLICABLE"
    RESERVADA = "RESERVADA"


class NivelRiesgo(StrEnum):
    BAJO = "BAJO"
    MEDIO = "MEDIO"
    ALTO = "ALTO"


class EstadoIdentidad(StrEnum):
    VERIFICADO = "VERIFICADO"
    RECHAZADO = "RECHAZADO"
    PENDIENTE = "PENDIENTE"


class RolActor(StrEnum):
    ASESOR = "ASESOR"
    SUSCRIPTOR = "SUSCRIPTOR"


@dataclass(frozen=True, slots=True)
class Actor:
    id: str
    rol: RolActor


@dataclass(frozen=True, slots=True)
class Regla:
    """Una regla de la tabla de decisión publicada por catalogo. `valor` es texto o, con `entre`
    y `en`, una tupla de textos."""

    codigo: str
    orden: int
    tipo: TipoRegla
    descripcion: str
    campo: str
    operador: str
    valor: str | tuple[str, ...]
    visibilidad: Visibilidad
    motivo_cliente: str


@dataclass(frozen=True, slots=True)
class ReglasSuscripcion:
    producto_id: str
    version: str
    hash: str
    reglas: tuple[Regla, ...]


@dataclass(frozen=True, slots=True)
class Cobertura:
    codigo: str
    nombre: str
    obligatoria: bool


@dataclass(frozen=True, slots=True)
class PerfilInsumos:
    id: UUID
    estado: str
    nivel_riesgo: NivelRiesgo | None
    confianza: Decimal
    fuentes_consultadas: int
    fuentes_declaradas: int


@dataclass(frozen=True, slots=True)
class InsumosSuscripcion:
    """Lo que `cotizacion` entrega de la oferta: sin fecha de nacimiento (solo la edad)."""

    cotizacion_id: UUID
    oferta_id: UUID | None
    codigo: str | None
    cliente_id: UUID
    estado_identidad: EstadoIdentidad
    socio_id: str | None
    producto_id: str
    version_producto: str
    moneda: str
    periodicidad: str
    prima_total: Decimal
    version_reglas_prima: str
    huella_calculo: str
    coberturas: tuple[Cobertura, ...]
    suma_asegurada: Decimal
    plazo_credito_meses: int
    edad: int
    calculado_en: datetime
    vigente_hasta: datetime
    perfil: PerfilInsumos | None
    coincidencias_listas_restrictivas: int | None


@dataclass(frozen=True, slots=True)
class ReglaEvaluada:
    regla: Regla
    # Texto del valor del caso; None si el insumo falta (NO_EVALUABLE).
    valor_caso: str | None
    resultado: ResultadoRegla


@dataclass(frozen=True, slots=True)
class Evaluacion:
    resultado: ResultadoDecision
    reglas: tuple[ReglaEvaluada, ...]
    regla_determinante: str | None
    motivos: tuple[str, ...]

    @property
    def reglas_no_cumplidas(self) -> tuple[str, ...]:
        return tuple(
            r.regla.codigo for r in self.reglas if r.resultado is not ResultadoRegla.CUMPLE
        )


@dataclass(frozen=True, slots=True)
class Decision:
    id: UUID
    codigo: str
    cotizacion_id: UUID
    oferta_id: UUID | None
    cotizacion_codigo: str | None
    cliente_id: UUID
    producto_id: str
    tipo: TipoDecision
    evaluacion: Evaluacion
    version_reglas: str
    hash_reglas: str
    insumos: InsumosSuscripcion
    hash_insumos: str
    decidida_en: datetime
    duracion_ms: int
    idempotency_key: UUID
    huella_solicitud: str
    correlation_id: str
    actor_id: str

    @property
    def resultado(self) -> ResultadoDecision:
        return self.evaluacion.resultado


@dataclass(frozen=True, slots=True)
class CasoBandeja:
    """Un caso en revisión de la bandeja (E01)."""

    decision_id: UUID
    codigo: str
    cotizacion_id: UUID
    cotizacion_codigo: str | None
    cliente_id: UUID
    producto_id: str
    moneda: str
    suma_asegurada: Decimal
    motivos: tuple[str, ...]
    reglas_no_cumplidas: tuple[str, ...]
    decidida_en: datetime
    # Se calcula al leer, con el reloj de polizas.
    antiguedad_minutos: int = 0


@dataclass(frozen=True, slots=True)
class FiltrosBandeja:
    producto_id: str | None = None
    motivo: str | None = None
    q: str | None = None


@dataclass(frozen=True, slots=True)
class PaginaBandeja:
    casos: tuple[CasoBandeja, ...]
    siguiente_cursor: str | None
    total: int


@dataclass(frozen=True, slots=True)
class Resumen:
    """Indicadores de E01 en [desde, hasta) (DS2-14)."""

    desde: datetime
    hasta: datetime
    resueltas_automaticamente: int
    enviadas_a_revision: int
    total: int
    tiempo_medio_decision_ms: int | None
    fuera_del_compromiso: int
    compromiso_revision_horas: int


@dataclass(frozen=True, slots=True)
class Reconstruccion:
    coincide: bool
    resultado_original: ResultadoDecision
    resultado_reconstruido: ResultadoDecision
    version_reglas: str
    hash_reglas: str


# --- Comunicación de la decisión (HU-W14) ----------------------------------------------------


class CondicionAseguramiento(StrEnum):
    APROBADA_SIN_AJUSTES = "APROBADA_SIN_AJUSTES"
    NO_ASEGURABLE = "NO_ASEGURABLE"
    EN_REVISION = "EN_REVISION"


class SiguientePaso(StrEnum):
    ACEPTAR_OFERTA = "ACEPTAR_OFERTA"
    COTIZAR_NUEVAMENTE = "COTIZAR_NUEVAMENTE"
    COTIZAR_ALTERNATIVA = "COTIZAR_ALTERNATIVA"
    ESPERAR_REVISION = "ESPERAR_REVISION"


@dataclass(frozen=True, slots=True)
class DescriptorAlternativa:
    """Lo que el portal muestra de la alternativa (E03): `EDAD_MAXIMA` con `valor` en años o
    `SIN_LIMITE_EDAD` sin valor."""

    codigo: str
    valor: str | None


@dataclass(frozen=True, slots=True)
class Alternativa:
    """Producto alternativo de una versión de reglas de suscripción; solo informativo."""

    producto_id: str
    descriptor: DescriptorAlternativa


@dataclass(frozen=True, slots=True)
class Comunicacion:
    """La decisión en términos del cliente (HU-W14). Nunca lleva el código ni el valor de una
    regla: solo motivos de un conjunto cerrado."""

    condicion: CondicionAseguramiento
    admite_contratacion: bool
    siguiente_paso: SiguientePaso
    # Solo en una aprobada (vigente o vencida).
    vigente_hasta: datetime | None
    # Rechazo: el de la regla determinante; revisión: R-12 (puede ser None).
    motivo_cliente: str | None
    # Solo en un rechazo.
    alternativas: tuple[Alternativa, ...]
    # Solo en una revisión.
    plazo_respuesta_horas: int | None
