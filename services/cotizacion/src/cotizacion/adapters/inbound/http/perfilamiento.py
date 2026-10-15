"""API interna /v1/perfiles (04 §B.4.2). La consume solo bff-web.

No expone `insumos`, `datosDeclarados`, valores observados ni el hash del modelo, salvo el
hash en /reconstruccion (minimización).
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common import correlation
from solventa_common.errors import ROL_NO_AUTORIZADO, SOLICITUD_INVALIDA, ErrorNegocio

from cotizacion.application.perfilamiento.crear_perfil import ComandoPerfil, CrearPerfil
from cotizacion.application.perfilamiento.puertos import RepositorioPerfiles
from cotizacion.application.perfilamiento.recalcular_perfil import RecalcularPerfil
from cotizacion.application.perfilamiento.reconstruir_perfil import ReconstruirPerfil
from cotizacion.domain.perfilamiento.errores import (
    ClienteNoEncontrado,
    ConflictoIdempotencia,
    DatosDeclaradosInvalidos,
    DependenciaNoDisponible,
    IdentidadNoVerificada,
    IdentidadPendiente,
    ModeloNoDisponible,
    PerfilNoEncontrado,
)
from cotizacion.domain.perfilamiento.modelos import (
    TIPO_DE_FUENTE,
    AccionSugerida,
    DatosDeclarados,
    DecisionConsentimiento,
    Direccion,
    EstadoFactor,
    EstadoFuente,
    EstadoPerfil,
    Fuente,
    MotivoDato,
    NivelRiesgo,
    Perfil,
    TipoFuente,
)

ROL_ASESOR = "ASESOR"


def actor_asesor(
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_rol: Annotated[str | None, Header(alias="X-Actor-Rol")] = None,
) -> str:
    """Actor de la solicitud. Se resuelve antes de validar el cuerpo: 401/403 van primero."""
    if not x_actor_id or not x_actor_rol:
        raise ErrorNegocio("ACTOR_REQUERIDO", "Faltan los encabezados del actor", 401)
    if x_actor_rol != ROL_ASESOR:
        raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
    return x_actor_id


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


_ERRORES_ACTOR: dict[int | str, dict[str, Any]] = {
    401: _error("ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
    403: _error("ROL_NO_AUTORIZADO: X-Actor-Rol distinto de ASESOR"),
}

router = APIRouter(
    prefix="/v1/perfiles",
    tags=["perfiles"],
    dependencies=[Depends(actor_asesor)],
    responses=_ERRORES_ACTOR,
)


# --- DTO ---------------------------------------------------------------------------------


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


class Reconstruccion(_Dto):
    coincide: bool
    version_modelo: str
    hash_modelo: str


def _float(valor: Decimal | None) -> float | None:
    return None if valor is None else float(valor)


def _utc(instante: datetime | None) -> datetime | None:
    return None if instante is None else instante.astimezone(UTC)


def respuesta_de(perfil: Perfil, presupuesto_ms: int) -> PerfilRespuesta:
    r = perfil.resultado
    consentimiento = perfil.consentimiento
    return PerfilRespuesta(
        id=perfil.id,
        cliente_id=perfil.cliente_id,
        producto_id=perfil.producto_id,
        estado=r.estado,
        nivel_riesgo=r.nivel,
        puntaje=_float(r.puntaje),
        confianza=float(r.confianza),
        factores=[
            FactorDto(
                codigo=f.codigo,
                fuente=f.fuente,
                peso=float(f.peso),
                estado=f.estado,
                contribucion=_float(f.contribucion),
                direccion=f.direccion,
                motivo=f.motivo,
            )
            for f in r.factores
        ],
        ajustes=[AjusteDto(codigo=a.codigo, efecto=a.efecto, nivel=a.nivel) for a in r.ajustes],
        datos_requeridos=[
            DatoRequeridoDto(campo=d.campo, fuente=d.fuente, motivo=d.motivo)
            for d in r.datos_requeridos
        ],
        accion_sugerida=r.accion,
        consentimiento=None
        if consentimiento is None
        else ConsentimientoDto(
            decision=consentimiento.decision,
            causa=consentimiento.causa,
            vigente_hasta=_utc(consentimiento.vigente_hasta),
        ),
        fuentes=[
            FuenteDto(
                fuente=f.fuente,
                tipo=TIPO_DE_FUENTE[f.fuente],
                estado=f.estado,
                version_fuente=f.version_fuente,
                confianza=_float(f.confianza),
                consultada_en=_utc(f.consultada_en),
                duracion_ms=f.duracion_ms,
            )
            for f in perfil.fuentes
        ],
        fuentes_consultadas=sum(1 for f in perfil.fuentes if f.estado is EstadoFuente.CONSULTADA),
        fuentes_declaradas=len(perfil.fuentes),
        version_modelo=perfil.version_modelo,
        calculado_en=perfil.calculado_en.astimezone(UTC),
        duracion_ms=perfil.duracion_ms,
        presupuesto_ms=presupuesto_ms,
        perfil_anterior_id=perfil.perfil_anterior_id,
    )


def resumen_de(perfil: Perfil) -> ResumenPerfil:
    return ResumenPerfil(
        id=perfil.id,
        estado=perfil.resultado.estado,
        nivel_riesgo=perfil.resultado.nivel,
        confianza=float(perfil.resultado.confianza),
        version_modelo=perfil.version_modelo,
        calculado_en=perfil.calculado_en.astimezone(UTC),
        perfil_anterior_id=perfil.perfil_anterior_id,
    )


# --- Errores (04 §B.7) -------------------------------------------------------------------


def error_negocio(error: Exception) -> ErrorNegocio:
    """Traduce los errores de dominio a respuestas B2 estables, sin datos del cliente."""
    match error:
        case DatosDeclaradosInvalidos(campos=campos):
            return ErrorNegocio(
                SOLICITUD_INVALIDA, "Solicitud inválida", 422, {"campos": list(campos)}
            )
        case ConflictoIdempotencia():
            return ErrorNegocio(
                "IDEMPOTENCIA_CONFLICTO",
                "La clave de idempotencia ya se usó con otra solicitud",
                422,
            )
        case ClienteNoEncontrado():
            return ErrorNegocio("CLIENTE_NO_ENCONTRADO", "El cliente no existe", 404)
        case IdentidadNoVerificada():
            return ErrorNegocio(
                "IDENTIDAD_NO_VERIFICADA", "La identidad del cliente no está verificada", 403
            )
        case IdentidadPendiente():
            return ErrorNegocio(
                "IDENTIDAD_PENDIENTE", "La verificación de identidad está pendiente", 409
            )
        case ModeloNoDisponible():
            return ErrorNegocio(
                "PRODUCTO_NO_DISPONIBLE", "El producto no tiene un modelo vigente", 422
            )
        case DependenciaNoDisponible(dependencia=dependencia):
            return ErrorNegocio(
                "DEPENDENCIA_NO_DISPONIBLE",
                "Una dependencia no está disponible",
                503,
                {"dependencia": dependencia},
            )
        case _:
            return ErrorNegocio("PERFIL_NO_ENCONTRADO", "El perfil no existe", 404)


ERRORES_TRADUCIBLES = (
    DatosDeclaradosInvalidos,
    ConflictoIdempotencia,
    ClienteNoEncontrado,
    IdentidadNoVerificada,
    IdentidadPendiente,
    ModeloNoDisponible,
    DependenciaNoDisponible,
    PerfilNoEncontrado,
)


# --- Rutas -------------------------------------------------------------------------------


def _crear_perfil(request: Request) -> CrearPerfil:
    caso: CrearPerfil = request.app.state.crear_perfil
    return caso


def _reconstruir_perfil(request: Request) -> ReconstruirPerfil:
    caso: ReconstruirPerfil = request.app.state.reconstruir_perfil
    return caso


def _recalcular_perfil(request: Request) -> RecalcularPerfil:
    caso: RecalcularPerfil = request.app.state.recalcular_perfil
    return caso


def _perfiles(request: Request) -> RepositorioPerfiles:
    repo: RepositorioPerfiles = request.app.state.perfiles
    return repo


@router.post(
    "",
    status_code=201,
    response_model=PerfilRespuesta,
    responses={
        200: {"model": PerfilRespuesta, "description": "Misma Idempotency-Key y mismo cuerpo"},
        403: _error("ROL_NO_AUTORIZADO o IDENTIDAD_NO_VERIFICADA"),
        404: _error("CLIENTE_NO_ENCONTRADO"),
        409: _error("IDENTIDAD_PENDIENTE"),
        422: _error("SOLICITUD_INVALIDA, IDEMPOTENCIA_CONFLICTO o PRODUCTO_NO_DISPONIBLE"),
        503: _error("DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia)"),
    },
)
async def crear(
    solicitud: SolicitudPerfil,
    respuesta: Response,
    actor: Annotated[str, Depends(actor_asesor)],
    crear_perfil: Annotated[CrearPerfil, Depends(_crear_perfil)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> PerfilRespuesta:
    """Calcula el perfil de riesgo del cliente con el modelo vigente del producto."""
    declarados = solicitud.datos_declarados
    comando = ComandoPerfil(
        cliente_id=solicitud.cliente_id,
        producto_id=solicitud.producto_id,
        datos_declarados=DatosDeclarados(
            fecha_nacimiento=declarados.fecha_nacimiento,
            plazo_credito_meses=declarados.plazo_credito_meses,
            suma_asegurada=Decimal(declarados.suma_asegurada),
        ),
        idempotency_key=idempotency_key,
        actor_id=actor,
        correlation_id=correlation.obtener() or "",
    )
    try:
        perfil, creado = await crear_perfil.ejecutar(comando)
    except ERRORES_TRADUCIBLES as error:
        raise error_negocio(error) from error
    if not creado:
        respuesta.status_code = 200
    return respuesta_de(perfil, crear_perfil.config.presupuesto_ms)


@router.post(
    "/{perfilId}/recalculos",
    status_code=201,
    response_model=PerfilRespuesta,
    responses={
        200: {"model": PerfilRespuesta, "description": "Misma Idempotency-Key"},
        403: _error("ROL_NO_AUTORIZADO o IDENTIDAD_NO_VERIFICADA"),
        404: _error("PERFIL_NO_ENCONTRADO o CLIENTE_NO_ENCONTRADO"),
        409: _error("IDENTIDAD_PENDIENTE"),
        422: _error("SOLICITUD_INVALIDA, IDEMPOTENCIA_CONFLICTO o PRODUCTO_NO_DISPONIBLE"),
        503: _error("DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia)"),
    },
)
async def recalcular(
    respuesta: Response,
    actor: Annotated[str, Depends(actor_asesor)],
    crear_perfil: Annotated[CrearPerfil, Depends(_crear_perfil)],
    recalcular_perfil: Annotated[RecalcularPerfil, Depends(_recalcular_perfil)],
    perfil_id: Annotated[UUID, Path(alias="perfilId")],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> PerfilRespuesta:
    """Crea un perfil nuevo desde uno anterior, con el modelo vigente; el anterior no cambia."""
    try:
        perfil, creado = await recalcular_perfil.ejecutar(
            perfil_id, idempotency_key, actor, correlation.obtener() or ""
        )
    except ERRORES_TRADUCIBLES as error:
        raise error_negocio(error) from error
    if not creado:
        respuesta.status_code = 200
    return respuesta_de(perfil, crear_perfil.config.presupuesto_ms)


@router.get(
    "",
    response_model=ListaPerfiles,
    responses={422: _error("SOLICITUD_INVALIDA")},
)
async def listar(
    perfiles: Annotated[RepositorioPerfiles, Depends(_perfiles)],
    cliente_id: Annotated[UUID, Query(alias="clienteId")],
    limite: Annotated[int, Query(ge=1, le=50)] = 20,
) -> ListaPerfiles:
    """Últimos perfiles del cliente, del más reciente al más antiguo."""
    return ListaPerfiles(
        items=[resumen_de(p) for p in await perfiles.por_cliente(cliente_id, limite)]
    )


@router.get(
    "/{perfilId}",
    response_model=PerfilRespuesta,
    responses={404: _error("PERFIL_NO_ENCONTRADO"), 422: _error("SOLICITUD_INVALIDA")},
)
async def obtener(
    perfiles: Annotated[RepositorioPerfiles, Depends(_perfiles)],
    crear_perfil: Annotated[CrearPerfil, Depends(_crear_perfil)],
    perfil_id: Annotated[UUID, Path(alias="perfilId")],
) -> PerfilRespuesta:
    """Un perfil calculado; los perfiles no cambian nunca."""
    perfil = await perfiles.por_id(perfil_id)
    if perfil is None:
        raise error_negocio(PerfilNoEncontrado())
    return respuesta_de(perfil, crear_perfil.config.presupuesto_ms)


@router.get(
    "/{perfilId}/reconstruccion",
    response_model=Reconstruccion,
    responses={404: _error("PERFIL_NO_ENCONTRADO"), 422: _error("SOLICITUD_INVALIDA")},
)
async def reconstruir(
    reconstruir_perfil: Annotated[ReconstruirPerfil, Depends(_reconstruir_perfil)],
    perfil_id: Annotated[UUID, Path(alias="perfilId")],
) -> Reconstruccion:
    """Vuelve a evaluar los insumos con la misma versión y dice si el resultado coincide."""
    try:
        coincide, version, hash_modelo = await reconstruir_perfil.ejecutar(perfil_id)
    except PerfilNoEncontrado as error:
        raise error_negocio(error) from error
    return Reconstruccion(coincide=coincide, version_modelo=version, hash_modelo=hash_modelo)
