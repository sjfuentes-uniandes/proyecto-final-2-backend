"""HU-W13: suscripción automática desde el portal (E01 a E04).

Los DTO son copia de los de polizas (contracts/openapi/polizas.yaml) con los mismos nombres:
tests/contract/test_openapi.py verifica que sus esquemas sean iguales. Las vistas
(`DecisionSuscripcionVista`, `CasoBandejaVista`) agregan `clienteNombre`, compuesto desde
clientes. `asesores` solicita y lee; `suscriptores` lee, ve la bandeja y sus indicadores (DS2-12).
"""

from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.errors import RutaB2

from bff_web.adapters.inbound.autorizacion import grupos, requerir_alguno, requerir_grupo
from bff_web.adapters.inbound.cotizaciones import CoberturaProductoDto
from bff_web.adapters.inbound.perfilamiento import NivelRiesgo, actor
from bff_web.adapters.inbound.socios import ErrorDto
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.polizas import ROL_ASESOR, ROL_SUSCRIPTOR, PolizasCliente
from bff_web.application.suscripcion import Suscripciones, servicio_no_disponible
from bff_web.config import settings

PATRON_PRODUCTO = r"^[a-z0-9-]{1,64}$"
TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]


# --- Enumeraciones y DTO (copia de polizas) -------------------------------------------------


class ResultadoDecision(StrEnum):
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"
    REVISION_REQUERIDA = "REVISION_REQUERIDA"


class TipoDecision(StrEnum):
    AUTOMATICA = "AUTOMATICA"


class TipoRegla(StrEnum):
    EXCLUYENTE = "EXCLUYENTE"
    REVISION = "REVISION"


class CampoRegla(StrEnum):
    EDAD = "edad"
    SUMA_ASEGURADA = "suma_asegurada"
    PLAZO_CREDITO_MESES = "plazo_credito_meses"
    NIVEL_RIESGO = "nivel_riesgo"
    CONFIANZA_PERFIL = "confianza_perfil"
    COINCIDENCIAS_LISTAS_RESTRICTIVAS = "coincidencias_listas_restrictivas"


class OperadorRegla(StrEnum):
    MAYOR_O_IGUAL = ">="
    MENOR_O_IGUAL = "<="
    IGUAL = "=="
    DISTINTO = "!="
    ENTRE = "entre"
    EN = "en"


class Visibilidad(StrEnum):
    EXPLICABLE = "EXPLICABLE"
    RESERVADA = "RESERVADA"


class ResultadoRegla(StrEnum):
    CUMPLE = "CUMPLE"
    NO_CUMPLE = "NO_CUMPLE"
    NO_EVALUABLE = "NO_EVALUABLE"


class MotivoCliente(StrEnum):
    EDAD_FUERA_DE_RANGO = "EDAD_FUERA_DE_RANGO"
    PLAZO_FUERA_DE_RANGO = "PLAZO_FUERA_DE_RANGO"
    SUMA_REQUIERE_REVISION = "SUMA_REQUIERE_REVISION"
    PERFIL_REQUIERE_REVISION = "PERFIL_REQUIERE_REVISION"
    NO_ELEGIBLE = "NO_ELEGIBLE"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SolicitudSuscripcion(_Dto):
    cotizacion_id: UUID


class ReglaEvaluadaDto(_Dto):
    codigo: str
    orden: int
    tipo: TipoRegla
    descripcion: str
    campo: CampoRegla
    operador: OperadorRegla
    valor: str | list[str]
    valor_caso: str | None = Field(
        description="Valor del caso como texto; null si no es evaluable o si la regla es "
        "RESERVADA y el actor no es SUSCRIPTOR (DS2-07)"
    )
    resultado: ResultadoRegla
    visibilidad: Visibilidad
    reservada: bool = Field(description="true si se ocultó valorCaso al actor (DS2-07)")


class CondicionesOfertaDto(_Dto):
    """Instantánea de la oferta al decidir (DS2-10), sin datos del perfil ni la edad."""

    socio_id: str | None
    version_producto: str
    moneda: str
    periodicidad: str
    prima_total: TextoDecimal
    version_reglas_prima: str
    huella_calculo: str
    coberturas: list[CoberturaProductoDto]
    suma_asegurada: TextoDecimal
    plazo_credito_meses: int
    calculado_en: datetime
    vigente_hasta: datetime


class ContextoCasoDto(_Dto):
    """Contexto del perfil para la revisión (E04); null en cada campo si no hubo perfil."""

    nivel_riesgo: NivelRiesgo | None
    confianza: TextoDecimal | None
    fuentes_consultadas: int | None
    fuentes_declaradas: int | None


class DecisionSuscripcion(_Dto):
    id: UUID
    codigo: Annotated[str, Field(pattern=r"^DEC-\d{4}-\d{6}$")]
    cotizacion_id: UUID
    oferta_id: UUID | None = Field(description="Null hasta HU-W04")
    cotizacion_codigo: str | None = Field(description="Null hasta HU-W04 (CF-S2-10)")
    cliente_id: UUID
    producto_id: str
    resultado: ResultadoDecision
    tipo: TipoDecision
    reglas_evaluadas: list[ReglaEvaluadaDto]
    regla_determinante: str | None = Field(
        description="Primera regla EXCLUYENTE incumplida (RECHAZADA); null en otro caso"
    )
    motivos: list[MotivoCliente]
    version_reglas: str
    hash_reglas: str
    condiciones: CondicionesOfertaDto
    contexto: ContextoCasoDto
    admite_excepcion: bool = Field(description="Siempre false en el Sprint 2 (DS2-08)")
    decidida_en: datetime
    duracion_ms: int


class ResumenDecision(_Dto):
    id: UUID
    codigo: str
    cotizacion_id: UUID
    resultado: ResultadoDecision
    version_reglas: str
    decidida_en: datetime


class ListaDecisiones(_Dto):
    items: list[ResumenDecision] = Field(description="0 o 1 elemento: una decisión por cotización")


class CasoBandeja(_Dto):
    decision_id: UUID
    codigo: str
    cotizacion_id: UUID
    cotizacion_codigo: str | None
    cliente_id: UUID
    producto_id: str
    moneda: str
    suma_asegurada: TextoDecimal
    motivos: list[MotivoCliente]
    reglas_no_cumplidas: list[str] = Field(
        description="Códigos de las reglas NO_CUMPLE o NO_EVALUABLE, por orden"
    )
    decidida_en: datetime
    antiguedad_minutos: int


class ResumenBandeja(_Dto):
    """Indicadores de E01 (DS2-14) en [desde, hasta)."""

    desde: datetime
    hasta: datetime
    resueltas_automaticamente: int
    enviadas_a_revision: int
    total: int
    tiempo_medio_decision_ms: int | None
    fuera_del_compromiso: int = Field(
        description="Casos en revisión con antigüedad mayor que el compromiso (de cualquier día)"
    )
    compromiso_revision_horas: int


# --- Vistas del portal ----------------------------------------------------------------------


class DecisionSuscripcionVista(DecisionSuscripcion):
    """La decisión de polizas más el nombre del cliente (E03, E04); null si clientes falla."""

    cliente_nombre: str | None


class CasoBandejaVista(CasoBandeja):
    cliente_nombre: str | None


class BandejaSuscripcionVista(_Dto):
    items: list[CasoBandejaVista] = Field(description="Del más antiguo al más reciente")
    siguiente_cursor: str | None
    total: int = Field(description="Casos en revisión que cumplen los filtros")


# --- Dependencias y rutas -------------------------------------------------------------------


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


_RESPUESTAS: dict[int | str, dict[str, Any]] = {
    401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
    403: _error("ROL_NO_AUTORIZADO: falta el grupo requerido o no es del back-office"),
    422: _error("SOLICITUD_INVALIDA (detalles.campos)"),
    503: _error("SERVICIO_NO_DISPONIBLE o DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia)"),
}
router = APIRouter(
    prefix="/v1/suscripciones", tags=["suscripciones"], route_class=RutaB2, responses=_RESPUESTAS
)
router_bandeja = APIRouter(
    prefix="/v1/bandeja-suscripcion",
    tags=["suscripciones"],
    route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_suscriptores))],
    responses=_RESPUESTAS,
)
ASESORES = [Depends(requerir_grupo(settings.grupo_asesores))]


@lru_cache
def suscripciones() -> Suscripciones:
    return Suscripciones(
        PolizasCliente(settings.polizas_url, settings.polizas_timeout_ms),
        ClientesCliente(settings.clientes_url, settings.clientes_timeout_ms),
        settings.nombre_cliente_timeout_ms,
    )


def rol_actor(
    x_authenticated_groups: Annotated[
        str | None, Header(alias="X-Authenticated-Groups", include_in_schema=False)
    ] = None,
) -> str:
    """SUSCRIPTOR si el usuario está en suscriptores (ve los valores reservados); si no, ASESOR."""
    if settings.grupo_suscriptores in grupos(x_authenticated_groups):
        return ROL_SUSCRIPTOR
    return ROL_ASESOR


Servicio = Annotated[Suscripciones, Depends(suscripciones)]
Actor = Annotated[str, Depends(actor)]
Rol = Annotated[str, Depends(rol_actor)]

_DECISION = TypeAdapter(DecisionSuscripcionVista)
_SOLICITUD = TypeAdapter(DecisionSuscripcion)
_LISTA = TypeAdapter(ListaDecisiones)
_BANDEJA = TypeAdapter(BandejaSuscripcionVista)
_RESUMEN = TypeAdapter(ResumenBandeja)


def _en_contrato(adaptador: TypeAdapter[Any], cuerpo: Any) -> Any:
    """Devuelve el cuerpo de polizas tal cual si cumple el DTO; si no, 503 (falla cerrado)."""
    try:
        adaptador.validate_python(cuerpo)
    except ValidationError as error:
        raise servicio_no_disponible() from error
    return cuerpo


@router.post(
    "",
    response_model=DecisionSuscripcion,
    status_code=201,
    dependencies=ASESORES,
    responses={
        200: {
            "model": DecisionSuscripcion,
            "description": "Misma Idempotency-Key, o la cotización ya tenía una decisión (DS2-05)",
        },
        403: _error("ROL_NO_AUTORIZADO o IDENTIDAD_NO_VERIFICADA"),
        404: _error("COTIZACION_NO_ENCONTRADA"),
        409: _error("OFERTA_VENCIDA (detalles.accion = NUEVA_COTIZACION) o IDENTIDAD_PENDIENTE"),
        422: _error("SOLICITUD_INVALIDA, IDEMPOTENCIA_CONFLICTO o REGLAS_NO_VIGENTES"),
    },
)
async def solicitar_suscripcion(
    solicitud: SolicitudSuscripcion,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    http: Response,
    servicio: Servicio,
    asesor: Actor,
) -> Any:
    """Pide la decisión automática de suscripción de una cotización (grupo asesores)."""
    estado, decision = await servicio.solicitar(
        solicitud.model_dump(mode="json", by_alias=True), asesor, idempotency_key
    )
    http.status_code = estado
    return _en_contrato(_SOLICITUD, decision)


@router.get("", response_model=ListaDecisiones, dependencies=ASESORES)
async def buscar_suscripcion(
    cotizacion_id: Annotated[UUID, Query(alias="cotizacionId")],
    servicio: Servicio,
    asesor: Actor,
) -> Any:
    """La decisión de una cotización, si existe (grupo asesores)."""
    return _en_contrato(_LISTA, await servicio.por_cotizacion(cotizacion_id, asesor))


@router.get(
    "/{decisionId}",
    response_model=DecisionSuscripcionVista,
    dependencies=[Depends(requerir_alguno(settings.grupo_asesores, settings.grupo_suscriptores))],
    responses={404: _error("DECISION_NO_ENCONTRADA")},
)
async def consultar_suscripcion(
    decision_id: Annotated[UUID, Path(alias="decisionId")],
    servicio: Servicio,
    usuario: Actor,
    rol: Rol,
) -> Any:
    """Una decisión (E02, E03, E04). Para asesores, las reglas RESERVADA llegan sin valorCaso."""
    return _en_contrato(_DECISION, await servicio.decision(decision_id, usuario, rol))


@router_bandeja.get("", response_model=BandejaSuscripcionVista)
async def bandeja_suscripcion(
    servicio: Servicio,
    suscriptor: Actor,
    producto_id: Annotated[str | None, Query(alias="productoId", pattern=PATRON_PRODUCTO)] = None,
    motivo: MotivoCliente | None = None,
    q: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limite: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> Any:
    """Casos en revisión del más antiguo al más reciente, con el nombre del cliente (E01)."""
    parametros = {
        "productoId": producto_id,
        "motivo": motivo.value if motivo else None,
        "q": q,
        "limite": limite,
        "cursor": cursor,
    }
    filtrados = {k: v for k, v in parametros.items() if v is not None}
    return _en_contrato(_BANDEJA, await servicio.bandeja(filtrados, suscriptor))


@router_bandeja.get(
    "/resumen",
    response_model=ResumenBandeja,
    responses={422: _error("SOLICITUD_INVALIDA (también si desde ≥ hasta)")},
)
async def resumen_bandeja(
    servicio: Servicio,
    suscriptor: Actor,
    desde: Annotated[AwareDatetime, Query(description="Inicio (incluido), ISO-8601 con zona")],
    hasta: Annotated[AwareDatetime, Query(description="Fin (excluido), ISO-8601 con zona")],
) -> Any:
    """Indicadores de la bandeja (DS2-14); `desde` y `hasta` delimitan el día del usuario."""
    parametros = {"desde": desde.isoformat(), "hasta": hasta.isoformat()}
    return _en_contrato(_RESUMEN, await servicio.resumen(parametros, suscriptor))
