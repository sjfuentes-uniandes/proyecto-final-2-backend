"""DTO de siniestros para el portal (HU-W19 y HU-W20): copia de `contracts/openapi/siniestros.yaml` con los
mismos nombres, campos y descripciones. `bff-web` solo agrega las vistas (`clienteNombre` y
`polizaVista`) en `siniestros.py`. `tests/contract/test_openapi.py` compara `bff-web.yaml` con la app.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel
from pydantic.alias_generators import to_camel

TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
CodigoSiniestro = Annotated[str, Field(pattern=r"^88-[0-9]{4}-[0-9]{4,}$")]
NumeroPoliza = Annotated[str, Field(pattern=r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$")]


# --- Enumeraciones ------------------------------------------------------------------------------


class EstadoSiniestro(StrEnum):
    REPORTADO = "REPORTADO"
    EN_EVALUACION = "EN_EVALUACION"
    APROBADO = "APROBADO"
    RECHAZADO = "RECHAZADO"
    FALTA_SOPORTE = "FALTA_SOPORTE"


class OrigenSiniestro(StrEnum):
    MOVIL = "MOVIL"
    PORTAL = "PORTAL"
    PARAMETRICO = "PARAMETRICO"


class PrioridadSiniestro(StrEnum):
    ALTA = "ALTA"
    MEDIA = "MEDIA"
    BAJA = "BAJA"


class CausalRechazo(StrEnum):
    """Causales contractuales del rechazo (H07), lista cerrada."""

    POLIZA_NO_VIGENTE = "POLIZA_NO_VIGENTE"
    EVENTO_NO_CUBIERTO = "EVENTO_NO_CUBIERTO"
    PERIODO_DE_CARENCIA = "PERIODO_DE_CARENCIA"
    AVISO_EXTEMPORANEO = "AVISO_EXTEMPORANEO"
    PRIMAS_EN_MORA = "PRIMAS_EN_MORA"
    EVIDENCIA_INCONSISTENTE = "EVIDENCIA_INCONSISTENTE"
    EXCLUSION_CONTRACTUAL = "EXCLUSION_CONTRACTUAL"


class SoporteSolicitable(StrEnum):
    """Documentos que el analista puede pedirle al cliente (H05): un subconjunto de `TipoSoporte`."""

    CERTIFICADO_REPARACION = "CERTIFICADO_REPARACION"
    COTIZACION_REPUESTOS = "COTIZACION_REPUESTOS"
    DENUNCIA_AUTORIDAD = "DENUNCIA_AUTORIDAD"
    PERITAJE = "PERITAJE"


class CanalSoporte(StrEnum):
    """Canal por el que se le pide el soporte al cliente (H05)."""

    PUSH_Y_CORREO = "PUSH_Y_CORREO"
    PUSH = "PUSH"
    CORREO = "CORREO"


class TipoBandeja(StrEnum):
    MIA = "MIA"
    SIN_ASIGNAR = "SIN_ASIGNAR"


class TipoEvidencia(StrEnum):
    FOTOGRAFIA = "FOTOGRAFIA"
    VIDEO = "VIDEO"
    SOPORTE = "SOPORTE"


class IntegridadEvidencia(StrEnum):
    VERIFICADA = "VERIFICADA"
    ALTERADA = "ALTERADA"
    NO_VERIFICABLE = "NO_VERIFICABLE"
    NO_DISPONIBLE = "NO_DISPONIBLE"


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


class EstadoPago(StrEnum):
    SIN_PAGO = "SIN_PAGO"
    PENDIENTE = "PENDIENTE"
    CONFIRMADO = "CONFIRMADO"
    RECHAZADO = "RECHAZADO"


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


# --- DTO ----------------------------------------------------------------------------------------


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


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
    ASIGNADO, APROBADO, RECHAZADO, SOPORTE_SOLICITADO, PLAZO_SOPORTE_VENCIDO; W22 y W23 agregan
    los suyos)."""

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
    """La liquidación aprobada (HU-W20). Null mientras el caso no esté APROBADO. En un paramétrico
    (W22) solo trae `valorIndemnizar`."""

    moneda: str = Field(pattern=r"^[A-Z]{3}$")
    valor_reclamado: TextoDecimal | None
    deducible: TextoDecimal | None
    valor_indemnizar: TextoDecimal | None
    referencia_pago: str | None = Field(
        description="IND-<codigo>-01, la clave idempotente del pago único (DS3-11)"
    )
    aprobada_en: datetime | None


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
    """Detalle (H03, H04, Cobertura e Historial). `liquidacion` la llena W20 al aprobar; las secciones
    de W22 y W23 vienen en null."""

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
    valor_reclamado: TextoDecimal | None = Field(
        description="Lo que reclamó el cliente (lo llena M01); null si aún no consta. Si consta, la "
        "liquidación aprobada debe coincidir"
    )
    referencia_pago_prevista: str = Field(
        pattern=r"^IND-88-[0-9]{4}-[0-9]{4,}-01$",
        description="IND-<codigo>-01: la referencia única que llevaría la orden de pago (H06)",
    )
    plazo_soporte_vence_en: datetime | None = Field(
        description="Solo en FALTA_SOPORTE: fin del plazo que tiene el cliente para responder"
    )


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


# --- Decisión del analista (HU-W20) -------------------------------------------------------------


class LiquidacionSolicitada(_Dto):
    """Liquidación que el analista aprueba (H03). Montos como texto decimal (DS2-16). Se exige
    `0 < valorIndemnizar ≤ valorReclamado`, `0 ≤ deducible ≤ valorReclamado`, `valorIndemnizar` ≤ suma
    asegurada del amparo reclamado, `moneda` igual a la de la póliza y, si el siniestro ya trae
    `valorReclamado`, que coincida. Si no, 422 VALOR_FUERA_DE_RANGO (`detalles.campos`)."""

    valor_reclamado: TextoDecimal
    deducible: TextoDecimal
    valor_indemnizar: TextoDecimal
    moneda: str = Field(pattern=r"^[A-Z]{3}$")


class _SolicitudBase(_Dto):
    model_config = ConfigDict(extra="forbid")

    justificacion: str = Field(
        max_length=2000,
        description="Obligatoria en toda decisión: vacía o solo espacios → 422 JUSTIFICACION_REQUERIDA",
    )
    evidencias_consideradas: list[UUID] = Field(
        default_factory=list,
        max_length=50,
        description="Evidencias del caso que el analista tuvo en cuenta; una que no sea del "
        "siniestro → 422 SOLICITUD_INVALIDA",
    )


class SolicitudAprobacion(_SolicitudBase):
    """APROBAR: el caso pasa a APROBADO y se ordena el pago único (`referenciaPago`)."""

    tipo: Literal["APROBAR"]
    liquidacion: LiquidacionSolicitada


class SolicitudRechazo(_SolicitudBase):
    """RECHAZAR: el caso pasa a RECHAZADO, no habilita pago y conserva la causal y la razón."""

    tipo: Literal["RECHAZAR"]
    causal: str = Field(
        max_length=40,
        description="Código de CausalRechazo (GET /v1/siniestros/catalogos-decision); otro valor "
        "→ 422 CAUSAL_INVALIDA",
    )
    explicacion_cliente: str = Field(
        max_length=2000,
        description="Texto que recibirá el cliente. Obligatoria: vacía → 422 SOLICITUD_INVALIDA. Se "
        "guarda con la decisión (dato sensible) y nunca viaja en un evento",
    )


class SolicitudSoporte(_SolicitudBase):
    """SOLICITAR_SOPORTE: el caso pasa a FALTA_SOPORTE y el reloj del compromiso se pausa."""

    tipo: Literal["SOLICITAR_SOPORTE"]
    documentos: list[SoporteSolicitable] = Field(min_length=1, max_length=4)
    mensaje_cliente: str = Field(
        max_length=1000,
        description="Texto que recibirá el cliente. Obligatorio: vacío → 422 SOLICITUD_INVALIDA. Se "
        "guarda con la decisión (dato sensible) y nunca viaja en un evento",
    )
    canal: CanalSoporte
    plazo_dias_habiles: Literal[5, 10, 15] = Field(
        description="Días hábiles (lunes a viernes, sin festivos) que tiene el cliente para responder"
    )


class SolicitudDecision(
    RootModel[
        Annotated[
            SolicitudAprobacion | SolicitudRechazo | SolicitudSoporte,
            Field(discriminator="tipo"),
        ]
    ]
):
    """Cuerpo de la decisión, discriminado por `tipo` (DS3-12)."""


class ResultadoDecisionSiniestro(_Dto):
    """Resultado de una decisión. La misma Idempotency-Key con el mismo cuerpo devuelve este mismo
    resultado, sin escribir nada nuevo."""

    siniestro_id: UUID
    estado_nuevo: EstadoSiniestro
    decision_id: UUID
    referencia_pago: str | None = Field(
        description="IND-<codigo>-01, la clave idempotente del pago (HU-W23); solo al aprobar"
    )
    plazo_soporte_vence_en: datetime | None = Field(
        description="Fin del plazo en días hábiles; solo al solicitar soporte"
    )
    registrada_en: datetime


class CatalogosDecision(_Dto):
    """Listas cerradas de la decisión, como códigos que el portal traduce (DS3-12)."""

    causales: list[CausalRechazo]
    documentos: list[SoporteSolicitable]
    canales: list[CanalSoporte]
    plazos_dias_habiles: list[int]
