"""DTO de `siniestros` (HU-W19): fuente de los esquemas de `contracts/openapi/siniestros.yaml`.

Los enumerados son los del dominio y de la aplicación (mismos nombres que en el contrato), así que
el esquema publicado no depende de una segunda copia. Los nombres, campos y descripciones de estas
clases son el contrato: `tests/contract/test_openapi.py` compara el YAML con `app.openapi()`.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from siniestros.application.siniestro.vistas import (
    MotivoDenegacion,
    RecursoSiniestro,
)
from siniestros.domain.siniestro.modelos import (
    CodigoVerificacion,
    EstadoPaso,
    EstadoSiniestro,
    EstadoSoporte,
    IntegridadEvidencia,
    OrigenSiniestro,
    PasoTramite,
    PrioridadSiniestro,
    ResultadoCoherencia,
    ResultadoVerificacion,
    TipoEvidencia,
    TipoSoporte,
)

TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
CodigoSiniestro = Annotated[str, Field(pattern=r"^88-[0-9]{4}-[0-9]{4,}$")]
NumeroPoliza = Annotated[str, Field(pattern=r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$")]


# --- Enumeraciones ------------------------------------------------------------------------------


class EstadoPago(StrEnum):
    SIN_PAGO = "SIN_PAGO"
    PENDIENTE = "PENDIENTE"
    CONFIRMADO = "CONFIRMADO"
    RECHAZADO = "RECHAZADO"


# --- DTO ----------------------------------------------------------------------------------------


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


class UbicacionSiniestroDto(_Dto):
    """Ubicación declarada o de captura: texto y/o coordenadas (WGS84)."""

    descripcion: str | None = Field(max_length=200)
    lat: float | None = Field(ge=-90, le=90)
    lon: float | None = Field(ge=-180, le=180)


class ResumenSiniestro(_Dto):
    """Fila de la bandeja (H01) y respuesta de «tomar el siguiente»."""

    id: UUID
    codigo: CodigoSiniestro
    poliza_id: UUID
    poliza_numero: NumeroPoliza
    cliente_id: UUID
    ramo: str = Field(description="Nombre del ramo (copia de la póliza al reportar)")
    origen: OrigenSiniestro
    estado: EstadoSiniestro
    prioridad: PrioridadSiniestro = Field(
        description="Derivada del tiempo restante: ALTA si está vencido o le quedan < 8 h; "
        "MEDIA si le quedan < 24 h; BAJA en otro caso (DS3-04)"
    )
    reportado_en: datetime
    analista_id: UUID | None = Field(description="Null mientras el caso no tiene analista")
    tiempo_restante_min: int = Field(
        description="Minutos que faltan para el compromiso de 48 h; negativo si está vencido"
    )
    reloj_pausado: bool = Field(description="true en FALTA_SOPORTE (el reloj se reanuda al volver)")


class BandejaSiniestrosRespuesta(_Dto):
    items: list[ResumenSiniestro] = Field(
        description="MIA: menor tiempo restante primero. SIN_ASIGNAR: el más antiguo primero"
    )
    siguiente_cursor: str | None
    total: int = Field(description="Casos que cumplen los filtros")


class ResumenBandejaSiniestros(_Dto):
    """Indicadores de H01 y H02. `desde`/`hasta` delimitan el día del usuario (UTC)."""

    pendientes: int = Field(
        description="EN_EVALUACION asignados al analista que consulta (esperan su evaluación)"
    )
    vencen_hoy: int = Field(
        description="EN_EVALUACION del analista cuyo compromiso vence en [desde, hasta)"
    )
    fuera_de_compromiso: int = Field(
        description="EN_EVALUACION del analista con tiempo restante < 0"
    )
    resueltos_semana: int = Field(
        description="APROBADO o RECHAZADO por el analista en los 7 días hasta `hasta`"
    )
    resueltos_semana_previa: int = Field(description="Ídem en los 7 días anteriores")
    tiempo_medio_decision_horas: float | None = Field(
        description="Promedio reportado → decidido de los resueltos de la semana; null sin datos"
    )
    compromiso_horas: int
    asignados_al_actor: int = Field(
        description="EN_EVALUACION y FALTA_SOPORTE asignados al analista que consulta"
    )
    sin_asignar: int = Field(description="REPORTADO sin analista (cola general)")


class AmparoCopiaDto(_Dto):
    codigo: str
    nombre: str
    suma_asegurada: TextoDecimal = Field(description="Suma asegurada del amparo (DS2-16)")


class VigenciaCopiaDto(_Dto):
    desde: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    hasta: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class BeneficiarioDto(_Dto):
    beneficiario_ref: str = Field(description="Identificador opaco; nunca la cuenta")
    cuenta_mascarada: str = Field(description="Cuenta enmascarada, p. ej. ****1234")


class CopiaPolizaDto(_Dto):
    """Copia de la póliza tomada al reportar (fuente de la verificación de cobertura)."""

    numero: NumeroPoliza
    ramo: str
    vigencia: VigenciaCopiaDto
    amparos: list[AmparoCopiaDto] = Field(
        description="Amparos contratados; `causa` es uno de ellos"
    )
    moneda: str = Field(pattern=r"^[A-Z]{3}$")
    carencia_dias: int = Field(ge=0)
    plazo_aviso_dias: int = Field(ge=0)
    primas_al_dia: bool
    beneficiario: BeneficiarioDto


class VerificacionCoberturaDto(_Dto):
    codigo: CodigoVerificacion
    resultado: ResultadoVerificacion
    detalle: dict[str, str] = Field(
        description="Parámetros para el texto del portal (nunca texto libre). VIGENCIA: fechaEvento, "
        "desde, hasta. AMPARO: amparo. CARENCIA: diasDesdeInicio, carenciaDias. PLAZO_AVISO: "
        "minutosDesdeEvento, plazoAvisoDias. PRIMAS: primasAlDia. SOPORTES: presentes, requeridos"
    )


class CoherenciaDto(_Dto):
    fecha_declarada: datetime
    primera_captura: datetime | None
    diferencia_min: int | None = Field(description="primeraCaptura − fechaDeclarada, en minutos")
    ubicacion_coincide: bool | None = Field(
        description="Distancia ≤ 1 km (configurable); null si falta alguna de las dos ubicaciones"
    )
    archivos_alterados: int
    resultado: ResultadoCoherencia


class EvidenciaVista(_Dto):
    """Metadatos de una evidencia. Nunca la clave del objeto ni una URL (DS3-08)."""

    id: UUID
    nombre_archivo: str
    tipo: TipoEvidencia
    capturada_en: datetime | None
    ubicacion: UbicacionSiniestroDto | None
    integridad: IntegridadEvidencia


class SoportePendienteDto(_Dto):
    tipo: TipoSoporte
    estado: EstadoSoporte


class PasoTramiteDto(_Dto):
    paso: PasoTramite
    estado: EstadoPaso
    ocurrido_en: datetime | None
    actor_nombre: str | None


class EntradaHistorial(_Dto):
    """Historial inmutable. `accion` es un código en mayúsculas (REPORTADO, EVIDENCIAS_VALIDADAS,
    ASIGNADO; W20 y W22 agregan los suyos)."""

    id: UUID
    accion: str = Field(pattern=r"^[A-Z_]{3,40}$")
    estado_anterior: EstadoSiniestro | None
    estado_nuevo: EstadoSiniestro | None
    actor_id: str | None
    actor_rol: str | None
    actor_nombre: str | None
    justificacion: str | None
    ocurrido_en: datetime


class LiquidacionDto(_Dto):
    """Null hasta HU-W20."""

    moneda: str = Field(pattern=r"^[A-Z]{3}$")
    valor_reclamado: TextoDecimal | None
    deducible: TextoDecimal | None
    valor_indemnizar: TextoDecimal | None


class EventoParametricoDto(_Dto):
    """Null hasta HU-W22."""

    evento_id: UUID
    fuente: str
    tipo_evento: str
    valor_observado: TextoDecimal
    umbral: TextoDecimal
    unidad: str
    zona: str
    observado_en: datetime
    regla: str
    version_regla: str


class PagoDto(_Dto):
    """Null hasta HU-W23."""

    estado: EstadoPago
    referencia: str | None
    transaccion_id: str | None
    motivo: str | None
    intentos: int
    duplicados_descartados: int


class DetalleSiniestro(ResumenSiniestro):
    """Detalle (H03, H04, Cobertura e Historial). Las secciones de W20, W22 y W23 vienen en null."""

    causa: str = Field(description="Código del amparo reclamado (uno de poliza.amparos)")
    fecha_evento: datetime
    ubicacion_declarada: UbicacionSiniestroDto
    poliza: CopiaPolizaDto
    verificacion_cobertura: list[VerificacionCoberturaDto] = Field(
        description="Seis verificaciones, en el orden de CodigoVerificacion"
    )
    coherencia: CoherenciaDto
    evidencias: list[EvidenciaVista]
    soportes_pendientes: list[SoportePendienteDto]
    tramite: list[PasoTramiteDto]
    historial: list[EntradaHistorial] = Field(description="Del más antiguo al más reciente")
    liquidacion: LiquidacionDto | None
    evento: EventoParametricoDto | None
    pago: PagoDto | None


class EnlaceEvidencia(_Dto):
    """URL prefirmada de lectura: expira en ≤ 300 s, no se guarda ni se registra (DS3-08)."""

    url: str
    expira_en: datetime
    tipo_contenido: str


class SolicitudAccesoDenegado(_Dto):
    actor_id: str = Field(min_length=1, max_length=128)
    recurso: RecursoSiniestro
    recurso_id: str | None = Field(max_length=64)
    motivo: MotivoDenegacion
