"""HU-W11: perfilamiento de riesgo desde el portal (04 §B.4.3). Solo el grupo asesores.

Los DTO son copia de los de cotizacion (contracts/openapi/cotizacion.yaml) con los mismos
nombres: tests/contract/test_openapi.py verifica que sus esquemas sean iguales.
"""

from datetime import date, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio, RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.socios import ErrorDto
from bff_web.adapters.outbound.cotizacion import CotizacionCliente
from bff_web.application.perfilamiento import Perfilamiento
from bff_web.config import settings


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/perfiles", tags=["perfiles"], route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_asesores))],
    responses={
        401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
        403: _error("ROL_NO_AUTORIZADO: falta el grupo asesores o no es del back-office"),
        422: _error("SOLICITUD_INVALIDA (detalles.campos)"),
        503: _error("SERVICIO_NO_DISPONIBLE o DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia)"),
    },
)


# --- Enumeraciones y DTO (copia de cotizacion) ----------------------------------------------


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


class DecisionConsentimiento(StrEnum):
    PERMITIDO = "PERMITIDO"
    PERMITIDO_PARCIAL = "PERMITIDO_PARCIAL"
    RECHAZADO = "RECHAZADO"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DatosDeclaradosDto(_Dto):
    fecha_nacimiento: date
    plazo_credito_meses: Annotated[int, Field(ge=12, le=360)]
    suma_asegurada: Annotated[int, Field(ge=1, le=5_000_000_000)]


class SolicitudPerfil(_Dto):
    cliente_id: UUID
    producto_id: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,64}$")]
    datos_declarados: DatosDeclaradosDto


class FactorDto(_Dto):
    codigo: str
    fuente: str
    peso: float
    estado: EstadoFactor
    contribucion: float | None
    direccion: Direccion | None
    motivo: MotivoDato | None


class AjusteDto(_Dto):
    codigo: str
    efecto: str
    nivel: NivelRiesgo | None


class DatoRequeridoDto(_Dto):
    campo: str
    fuente: Fuente | None
    motivo: MotivoDato


class ConsentimientoDto(_Dto):
    decision: DecisionConsentimiento
    causa: str | None
    vigente_hasta: datetime | None


class FuenteDto(_Dto):
    fuente: Fuente
    tipo: TipoFuente
    estado: EstadoFuente
    version_fuente: str | None
    confianza: float | None
    consultada_en: datetime | None
    duracion_ms: int | None


class PerfilRespuesta(_Dto):
    id: UUID
    cliente_id: UUID
    producto_id: str
    estado: EstadoPerfil
    nivel_riesgo: NivelRiesgo | None
    puntaje: float | None
    confianza: float
    factores: list[FactorDto]
    ajustes: list[AjusteDto]
    datos_requeridos: list[DatoRequeridoDto]
    accion_sugerida: AccionSugerida | None
    consentimiento: ConsentimientoDto | None
    fuentes: list[FuenteDto]
    fuentes_consultadas: int
    fuentes_declaradas: int
    version_modelo: str
    calculado_en: datetime
    duracion_ms: int
    presupuesto_ms: int
    perfil_anterior_id: UUID | None


class ResumenPerfil(_Dto):
    id: UUID
    estado: EstadoPerfil
    nivel_riesgo: NivelRiesgo | None
    confianza: float
    version_modelo: str
    calculado_en: datetime
    perfil_anterior_id: UUID | None


class ListaPerfiles(_Dto):
    items: list[ResumenPerfil]


# --- Dependencias --------------------------------------------------------------------------


@lru_cache
def perfilamiento() -> Perfilamiento:
    return Perfilamiento(CotizacionCliente(settings.cotizacion_url, settings.cotizacion_timeout_ms))


def actor(
    x_authenticated_sub: Annotated[str | None, Header(alias="X-Authenticated-Sub", include_in_schema=False)] = None,
) -> str:
    """El asesor autenticado: API Gateway pone su `sub` en X-Authenticated-Sub (DT-17)."""
    if not x_authenticated_sub:
        raise ErrorNegocio("ACTOR_REQUERIDO", "No se identificó al usuario autenticado", 401)
    return x_authenticated_sub


Servicio = Annotated[Perfilamiento, Depends(perfilamiento)]
Actor = Annotated[str, Depends(actor)]
IdPerfil = Annotated[UUID, Path(alias="perfilId")]


# --- Rutas ---------------------------------------------------------------------------------


@router.post(
    "", response_model=PerfilRespuesta, status_code=201,
    responses={
        200: {"model": PerfilRespuesta, "description": "Misma Idempotency-Key y mismo cuerpo"},
        403: _error("ROL_NO_AUTORIZADO o IDENTIDAD_NO_VERIFICADA"),
        404: _error("CLIENTE_NO_ENCONTRADO"),
        409: _error("IDENTIDAD_PENDIENTE"),
        422: _error("SOLICITUD_INVALIDA, IDEMPOTENCIA_CONFLICTO o PRODUCTO_NO_DISPONIBLE"),
    },
)
async def crear_perfil(
    solicitud: SolicitudPerfil, http: Response, servicio: Servicio, asesor: Actor,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> dict[str, Any]:
    """Calcula el perfil de riesgo del cliente (paso 1 del perfilamiento)."""
    estado, perfil = await servicio.crear(solicitud.model_dump(mode="json", by_alias=True), asesor, idempotency_key)
    http.status_code = estado
    return perfil


@router.get("", response_model=ListaPerfiles)
async def listar_perfiles(
    servicio: Servicio, asesor: Actor,
    cliente_id: Annotated[UUID, Query(alias="clienteId")],
    limite: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Últimos perfiles del cliente, del más reciente al más antiguo."""
    return await servicio.listar(cliente_id, limite, asesor)


@router.get("/{perfilId}", response_model=PerfilRespuesta, responses={404: _error("PERFIL_NO_ENCONTRADO")})
async def consultar_perfil(perfil_id: IdPerfil, servicio: Servicio, asesor: Actor) -> dict[str, Any]:
    """Un perfil calculado (D02)."""
    return await servicio.obtener(perfil_id, asesor)
