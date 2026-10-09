"""Modelos del perfilamiento (02, CS-05). Puros: sin Pydantic ni E/S."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID


class Fuente(StrEnum):
    FA_PRODUCTOS_VIGENTES = "FA_PRODUCTOS_VIGENTES"
    FA_HISTORIAL_PAGOS_12M = "FA_HISTORIAL_PAGOS_12M"
    FA_INGRESOS_AGREGADOS = "FA_INGRESOS_AGREGADOS"
    DA_REGISTROS_PUBLICOS = "DA_REGISTROS_PUBLICOS"


class TipoFuente(StrEnum):
    FINANZAS_ABIERTAS = "FINANZAS_ABIERTAS"
    DATOS_ABIERTOS = "DATOS_ABIERTOS"
    DECLARADO = "DECLARADO"


class EstadoFuente(StrEnum):
    CONSULTADA = "CONSULTADA"
    NO_AUTORIZADA = "NO_AUTORIZADA"
    NO_CONSULTADA = "NO_CONSULTADA"
    TIEMPO_AGOTADO = "TIEMPO_AGOTADO"
    NO_DISPONIBLE = "NO_DISPONIBLE"
    RESPUESTA_INVALIDA = "RESPUESTA_INVALIDA"


class EstadoCampo(StrEnum):
    DISPONIBLE = "DISPONIBLE"
    NO_DISPONIBLE = "NO_DISPONIBLE"


class EstadoPerfil(StrEnum):
    CALCULADO = "CALCULADO"
    DEGRADADO = "DEGRADADO"
    INFORMACION_INSUFICIENTE = "INFORMACION_INSUFICIENTE"


class NivelRiesgo(StrEnum):
    BAJO = "BAJO"
    MEDIO = "MEDIO"
    ALTO = "ALTO"


class Direccion(StrEnum):
    REDUCE_RIESGO = "REDUCE_RIESGO"
    NEUTRO = "NEUTRO"
    AUMENTA_LEVEMENTE = "AUMENTA_LEVEMENTE"
    AUMENTA_RIESGO = "AUMENTA_RIESGO"


class EstadoFactor(StrEnum):
    EVALUADO = "EVALUADO"
    NO_EVALUADO = "NO_EVALUADO"


class MotivoDato(StrEnum):
    NO_AUTORIZADO = "NO_AUTORIZADO"
    NO_DISPONIBLE_EN_FUENTE = "NO_DISPONIBLE_EN_FUENTE"
    TIEMPO_AGOTADO = "TIEMPO_AGOTADO"
    FUENTE_NO_DISPONIBLE = "FUENTE_NO_DISPONIBLE"
    RESPUESTA_INVALIDA = "RESPUESTA_INVALIDA"


class AccionSugerida(StrEnum):
    COMPLETAR_CONSENTIMIENTO = "COMPLETAR_CONSENTIMIENTO"
    RECALCULAR = "RECALCULAR"


class EstadoIdentidad(StrEnum):
    VERIFICADO = "VERIFICADO"
    RECHAZADO = "RECHAZADO"
    PENDIENTE = "PENDIENTE"


class DecisionConsentimiento(StrEnum):
    PERMITIDO = "PERMITIDO"
    PERMITIDO_PARCIAL = "PERMITIDO_PARCIAL"
    RECHAZADO = "RECHAZADO"


PROPOSITO_PERFILAMIENTO = "PERFILAMIENTO_PRECIO"
PRODUCTO_VIDA_HIPOTECARIO = "vida-hipotecario"

TIPO_DE_FUENTE: Mapping[Fuente, TipoFuente] = MappingProxyType(
    {
        Fuente.FA_PRODUCTOS_VIGENTES: TipoFuente.FINANZAS_ABIERTAS,
        Fuente.FA_HISTORIAL_PAGOS_12M: TipoFuente.FINANZAS_ABIERTAS,
        Fuente.FA_INGRESOS_AGREGADOS: TipoFuente.FINANZAS_ABIERTAS,
        Fuente.DA_REGISTROS_PUBLICOS: TipoFuente.DATOS_ABIERTOS,
    }
)


@dataclass(frozen=True, slots=True)
class ValorCampo:
    estado: EstadoCampo
    valor: Decimal | None


@dataclass(frozen=True, slots=True)
class SolicitudFuente:
    fuente: Fuente
    campos: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextoConsulta:
    cliente_id: UUID
    consentimiento_id: UUID | None
    proposito: str
    deadline_ms: int


@dataclass(frozen=True, slots=True)
class ResultadoFuente:
    fuente: Fuente
    estado: EstadoFuente
    campos_solicitados: tuple[str, ...]
    campos: Mapping[str, ValorCampo]
    version_fuente: str | None
    confianza: Decimal | None
    consultada_en: datetime | None
    duracion_ms: int | None
    proveedor: str | None


@dataclass(frozen=True, slots=True)
class DatosDeclarados:
    fecha_nacimiento: date
    plazo_credito_meses: int
    suma_asegurada: Decimal


@dataclass(frozen=True, slots=True)
class VerificacionConsentimiento:
    decision: DecisionConsentimiento
    consentimiento_id: UUID | None
    vigente_hasta: datetime | None
    alcances_permitidos: frozenset[Fuente]
    causa: str | None


@dataclass(frozen=True, slots=True)
class PlanFuentes:
    a_consultar: tuple[SolicitudFuente, ...]
    no_autorizadas: tuple[Fuente, ...]
    no_consultadas: tuple[Fuente, ...]


@dataclass(frozen=True, slots=True)
class FactorEvaluado:
    codigo: str
    fuente: str
    peso: Decimal
    estado: EstadoFactor
    contribucion: Decimal | None
    direccion: Direccion | None
    motivo: MotivoDato | None


@dataclass(frozen=True, slots=True)
class AjusteAplicado:
    codigo: str
    efecto: str
    nivel: NivelRiesgo | None


@dataclass(frozen=True, slots=True)
class DatoRequerido:
    campo: str
    fuente: Fuente | None
    motivo: MotivoDato


@dataclass(frozen=True, slots=True)
class ResultadoEvaluacion:
    estado: EstadoPerfil
    nivel: NivelRiesgo | None
    puntaje: Decimal | None
    confianza: Decimal
    factores: tuple[FactorEvaluado, ...]
    ajustes: tuple[AjusteAplicado, ...]
    datos_requeridos: tuple[DatoRequerido, ...]
    accion: AccionSugerida | None


@dataclass(frozen=True, slots=True)
class Perfil:
    id: UUID
    cliente_id: UUID
    producto_id: str
    idempotency_key: UUID
    huella_solicitud: str
    datos_declarados: DatosDeclarados
    insumos: Mapping[str, Decimal]
    resultado: ResultadoEvaluacion
    fuentes: tuple[ResultadoFuente, ...]
    consentimiento: VerificacionConsentimiento | None
    version_modelo: str
    hash_modelo: str
    perfil_anterior_id: UUID | None
    calculado_en: datetime
    duracion_ms: int
    correlation_id: str
    actor_id: str
