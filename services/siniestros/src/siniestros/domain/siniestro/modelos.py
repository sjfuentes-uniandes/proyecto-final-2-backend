"""Modelo del siniestro (HU-W19; DS3-02 a DS3-06). Puro: sin Pydantic, sin E/S y sin reloj.

- `Siniestro` es inmutable: un cambio de estado devuelve otro (`asignar`, en `transiciones`).
- Los enumerados llevan los nombres del contrato (`siniestros.yaml`): el adaptador HTTP los reutiliza
  en sus DTO y así el esquema publicado no depende de una segunda copia.
- Los nombres de **clientes** no existen aquí: los compone `bff-web` desde `clientes`. El nombre del
  analista solo vive en el historial (`actor_nombre`).
- `Evidencia.clave_objeto` nunca sale del servicio (DS3-08): ninguna vista del dominio la expone.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from siniestros.domain.siniestro.errores import SiniestroInvalido

# El mismo patrón que el contrato (`CodigoSiniestro`) y que el CHECK de la migración 001.
CODIGO_SINIESTRO = re.compile(r"^88-[0-9]{4}-[0-9]{4,}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EstadoSiniestro(StrEnum):
    REPORTADO = "REPORTADO"
    EN_EVALUACION = "EN_EVALUACION"
    APROBADO = "APROBADO"
    RECHAZADO = "RECHAZADO"
    FALTA_SOPORTE = "FALTA_SOPORTE"


# Con analista: el caso está asignado y espera la evaluación o un soporte.
ESTADOS_CON_ANALISTA = frozenset({EstadoSiniestro.EN_EVALUACION, EstadoSiniestro.FALTA_SOPORTE})
# La decisión detiene el reloj en `decidido_en`.
ESTADOS_DECIDIDOS = frozenset({EstadoSiniestro.APROBADO, EstadoSiniestro.RECHAZADO})


class OrigenSiniestro(StrEnum):
    MOVIL = "MOVIL"
    PORTAL = "PORTAL"
    PARAMETRICO = "PARAMETRICO"


class PrioridadSiniestro(StrEnum):
    ALTA = "ALTA"
    MEDIA = "MEDIA"
    BAJA = "BAJA"


class TipoEvidencia(StrEnum):
    FOTOGRAFIA = "FOTOGRAFIA"
    VIDEO = "VIDEO"
    SOPORTE = "SOPORTE"


class IntegridadEvidencia(StrEnum):
    VERIFICADA = "VERIFICADA"
    ALTERADA = "ALTERADA"
    NO_VERIFICABLE = "NO_VERIFICABLE"
    NO_DISPONIBLE = "NO_DISPONIBLE"


class TipoSoporte(StrEnum):
    DOCUMENTO_IDENTIDAD = "DOCUMENTO_IDENTIDAD"
    FACTURA_COMPRA = "FACTURA_COMPRA"
    CERTIFICADO_REPARACION = "CERTIFICADO_REPARACION"
    COTIZACION_REPUESTOS = "COTIZACION_REPUESTOS"
    DENUNCIA_AUTORIDAD = "DENUNCIA_AUTORIDAD"
    PERITAJE = "PERITAJE"


class EstadoSoporte(StrEnum):
    FALTA = "FALTA"
    SOLICITADO = "SOLICITADO"


class CodigoVerificacion(StrEnum):
    VIGENCIA = "VIGENCIA"
    AMPARO = "AMPARO"
    CARENCIA = "CARENCIA"
    PLAZO_AVISO = "PLAZO_AVISO"
    PRIMAS = "PRIMAS"
    SOPORTES = "SOPORTES"


class ResultadoVerificacion(StrEnum):
    CUMPLE = "CUMPLE"
    NO_CUMPLE = "NO_CUMPLE"
    PARCIAL = "PARCIAL"
    NO_EVALUABLE = "NO_EVALUABLE"


class ResultadoCoherencia(StrEnum):
    COHERENTE = "COHERENTE"
    INCONSISTENTE = "INCONSISTENTE"
    NO_EVALUABLE = "NO_EVALUABLE"


class PasoTramite(StrEnum):
    RECIBIDO = "RECIBIDO"
    EVIDENCIAS_VALIDADAS = "EVIDENCIAS_VALIDADAS"
    EN_EVALUACION = "EN_EVALUACION"
    DECISION = "DECISION"
    PAGO_INDEMNIZACION = "PAGO_INDEMNIZACION"


class EstadoPaso(StrEnum):
    COMPLETADO = "COMPLETADO"
    ACTUAL = "ACTUAL"
    PENDIENTE = "PENDIENTE"


def con_zona(instante: object) -> bool:
    return isinstance(instante, datetime) and instante.utcoffset() is not None


@dataclass(frozen=True)
class Ubicacion:
    """Ubicación declarada o de captura: texto y/o coordenadas WGS84."""

    descripcion: str | None = None
    lat: float | None = None
    lon: float | None = None

    @property
    def tiene_coordenadas(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True)
class Amparo:
    codigo: str
    nombre: str
    suma_asegurada: str  # texto decimal (DS2-16); el dinero no pasa por float


@dataclass(frozen=True)
class Vigencia:
    desde: date
    hasta: date


@dataclass(frozen=True)
class Beneficiario:
    beneficiario_ref: str  # opaco: nunca la cuenta
    cuenta_mascarada: str


@dataclass(frozen=True)
class CopiaPoliza:
    """La póliza tal como estaba al reportar (fuente de la verificación de cobertura).

    Los campos que pueden faltar en la copia (`None`) vuelven `NO_EVALUABLE` la verificación que
    los usa; no se asume un valor.
    """

    numero: str
    ramo: str
    moneda: str
    amparos: tuple[Amparo, ...] = ()
    vigencia: Vigencia | None = None
    carencia_dias: int | None = None
    plazo_aviso_dias: int | None = None
    primas_al_dia: bool | None = None
    beneficiario: Beneficiario | None = None


@dataclass(frozen=True)
class EventoReclamado:
    """Lo que el cliente declaró: el amparo reclamado, cuándo ocurrió y cuándo lo reportó."""

    causa: str
    fecha_evento: datetime
    reportado_en: datetime


@dataclass(frozen=True)
class Evidencia:
    id: UUID
    siniestro_id: UUID
    nombre_archivo: str
    tipo: TipoEvidencia
    clave_objeto: str = field(repr=False)  # nunca sale del servicio ni se registra
    sha256: str = field(repr=False)
    capturada_en: datetime | None = None
    ubicacion: Ubicacion | None = None
    soporte_tipo: TipoSoporte | None = None
    tamano_bytes: int | None = None
    tipo_contenido: str | None = None

    def __post_init__(self) -> None:
        if (self.tipo is TipoEvidencia.SOPORTE) != (self.soporte_tipo is not None):
            raise SiniestroInvalido("soporte_tipo: solo y siempre en una evidencia SOPORTE")
        if SHA256.fullmatch(self.sha256) is None:
            raise SiniestroInvalido("sha256: debe tener 64 caracteres hexadecimales en minúscula")
        if self.capturada_en is not None and not con_zona(self.capturada_en):
            raise SiniestroInvalido("capturada_en: debe traer zona horaria")


@dataclass(frozen=True)
class EntradaHistorial:
    """Fila del historial inmutable (DS3-14). `accion` es un código: W20 y W22 agregan los suyos."""

    id: UUID
    siniestro_id: UUID
    accion: str
    ocurrido_en: datetime
    estado_anterior: EstadoSiniestro | None = None
    estado_nuevo: EstadoSiniestro | None = None
    actor_id: str | None = None
    actor_rol: str | None = None
    actor_nombre: str | None = None
    justificacion: str | None = None


@dataclass(frozen=True)
class Siniestro:
    id: UUID
    codigo: str
    origen: OrigenSiniestro
    estado: EstadoSiniestro
    poliza_id: UUID
    cliente_id: UUID
    copia_poliza: CopiaPoliza
    causa: str
    fecha_evento: datetime
    ubicacion_declarada: Ubicacion
    reportado_en: datetime
    correlation_id: str
    analista_id: UUID | None = None
    asignado_en: datetime | None = None
    pausado_desde: datetime | None = None
    pausa_acumulada_s: int = 0
    plazo_soporte_vence_en: datetime | None = None
    decidido_en: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if CODIGO_SINIESTRO.fullmatch(self.codigo) is None:
            raise SiniestroInvalido("codigo: debe tener la forma 88-AAAA-NNNN")
        for nombre in (
            "fecha_evento",
            "reportado_en",
            "asignado_en",
            "pausado_desde",
            "plazo_soporte_vence_en",
            "decidido_en",
        ):
            valor = getattr(self, nombre)
            if valor is not None and not con_zona(valor):
                raise SiniestroInvalido(f"{nombre}: debe traer zona horaria")
        if self.pausa_acumulada_s < 0:
            raise SiniestroInvalido("pausa_acumulada_s: no puede ser negativa")
        # Un caso decidido puede no tener analista (lo aprueba un paramétrico, W22).
        if self.estado in ESTADOS_CON_ANALISTA and self.analista_id is None:
            raise SiniestroInvalido("analista_id: obligatorio en EN_EVALUACION y FALTA_SOPORTE")
        if self.estado is EstadoSiniestro.REPORTADO and self.analista_id is not None:
            raise SiniestroInvalido("analista_id: un caso REPORTADO no tiene analista")
        if (self.estado is EstadoSiniestro.FALTA_SOPORTE) != (self.pausado_desde is not None):
            raise SiniestroInvalido("pausado_desde: el reloj solo se pausa en FALTA_SOPORTE")
        if (self.estado in ESTADOS_DECIDIDOS) != (self.decidido_en is not None):
            raise SiniestroInvalido("decidido_en: solo un caso APROBADO o RECHAZADO")

    @property
    def reloj_pausado(self) -> bool:
        return self.estado is EstadoSiniestro.FALTA_SOPORTE
