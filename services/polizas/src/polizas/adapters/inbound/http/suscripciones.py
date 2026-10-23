"""API interna de la suscripción (HU-W13, W13-B18): /v1/suscripciones. La consume bff-web.

Actores (X-Actor-Id, X-Actor-Rol): `ASESOR` solicita y lee; `SUSCRIPTOR` lee, ve la bandeja y sus
indicadores. Para `ASESOR`, las reglas RESERVADA llegan sin `valorCaso` (DS2-07). Montos como texto
decimal (DS2-16). Errores en formato B2, sin detalles internos.

HU-W14: la decisión que devuelven `POST /v1/suscripciones` y `GET /v1/suscripciones/{decisionId}`
trae `comunicacion`, derivada al leer (campo opcional: un cliente de la versión W13 sigue
validando). No lleva códigos ni valores de reglas.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common import correlation
from solventa_common.errors import ROL_NO_AUTORIZADO, ErrorNegocio

from polizas.application.suscripcion.comunicar import ComunicarDecision
from polizas.application.suscripcion.consultar import ConsultarSuscripciones
from polizas.application.suscripcion.reconstruir import ReconstruirDecision
from polizas.application.suscripcion.solicitar_suscripcion import (
    ComandoSuscripcion,
    SolicitarSuscripcion,
)
from polizas.domain.suscripcion.errores import (
    ConflictoIdempotencia,
    CotizacionNoEncontrada,
    CursorInvalido,
    DecisionNoEncontrada,
    DependenciaNoDisponible,
    IdentidadNoVerificada,
    IdentidadPendiente,
    OfertaVencida,
    RangoInvalido,
    ReglasNoVigentes,
)
from polizas.domain.suscripcion.insumos import texto
from polizas.domain.suscripcion.modelos import (
    Actor,
    Comunicacion,
    CondicionAseguramiento,
    Decision,
    FiltrosBandeja,
    NivelRiesgo,
    ResultadoDecision,
    ResultadoRegla,
    RolActor,
    SiguientePaso,
    TipoDecision,
    TipoRegla,
    Visibilidad,
)
from polizas.domain.suscripcion.modelos import (
    CasoBandeja as Caso,
)

PATRON_PRODUCTO = r"^[a-z0-9-]{1,64}$"
TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]


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


class MotivoCliente(StrEnum):
    EDAD_FUERA_DE_RANGO = "EDAD_FUERA_DE_RANGO"
    PLAZO_FUERA_DE_RANGO = "PLAZO_FUERA_DE_RANGO"
    SUMA_REQUIERE_REVISION = "SUMA_REQUIERE_REVISION"
    PERFIL_REQUIERE_REVISION = "PERFIL_REQUIERE_REVISION"
    NO_ELEGIBLE = "NO_ELEGIBLE"


class CodigoDescriptor(StrEnum):
    EDAD_MAXIMA = "EDAD_MAXIMA"
    SIN_LIMITE_EDAD = "SIN_LIMITE_EDAD"


# --- DTO (bff-web copia los mismos, con los mismos nombres) ----------------------------------


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


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


class CoberturaProductoDto(_Dto):
    codigo: str
    nombre: str
    obligatoria: bool


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


class DescriptorAlternativaDto(_Dto):
    codigo: CodigoDescriptor
    valor: TextoDecimal | None = Field(description="Años con EDAD_MAXIMA; null con SIN_LIMITE_EDAD")


class AlternativaDto(_Dto):
    producto_id: str
    descriptor: DescriptorAlternativaDto


class ComunicacionDto(_Dto):
    """La decisión en términos del cliente (HU-W14). Sin códigos ni valores de reglas."""

    condicion: CondicionAseguramiento
    admite_contratacion: bool = Field(
        description="true solo en una APROBADA con la oferta vigente: el portal ofrece aceptar"
    )
    siguiente_paso: SiguientePaso
    vigente_hasta: datetime | None = Field(
        description="Fin de la vigencia de la oferta; solo en APROBADA_SIN_AJUSTES"
    )
    motivo_cliente: MotivoCliente | None = Field(
        description="NO_ASEGURABLE: motivo de la regla determinante (NO_ELEGIBLE si es "
        "reservada). EN_REVISION: el de la primera regla explicable pendiente, o null (R-12)"
    )
    alternativas: list[AlternativaDto] = Field(
        description="Solo en NO_ASEGURABLE; vacía si catalogo no respondió"
    )
    plazo_respuesta_horas: int | None = Field(description="Solo en EN_REVISION")


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
    comunicacion: ComunicacionDto | None = Field(
        None, description="Comunicación de la decisión (HU-W14), derivada al leer"
    )


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


class BandejaSuscripcion(_Dto):
    items: list[CasoBandeja] = Field(description="Del más antiguo al más reciente")
    siguiente_cursor: str | None
    total: int = Field(description="Casos en revisión que cumplen los filtros")


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


class ReconstruccionDecision(_Dto):
    coincide: bool
    resultado_original: ResultadoDecision
    resultado_reconstruido: ResultadoDecision
    version_reglas: str
    hash_reglas: str


# --- Dominio → DTO ---------------------------------------------------------------------------


def comunicacion_dto(comunicacion: Comunicacion) -> ComunicacionDto:
    motivo = comunicacion.motivo_cliente
    return ComunicacionDto(
        condicion=comunicacion.condicion,
        admite_contratacion=comunicacion.admite_contratacion,
        siguiente_paso=comunicacion.siguiente_paso,
        vigente_hasta=comunicacion.vigente_hasta,
        motivo_cliente=None if motivo is None else MotivoCliente(motivo),
        alternativas=[
            AlternativaDto(
                producto_id=a.producto_id,
                descriptor=DescriptorAlternativaDto(
                    codigo=CodigoDescriptor(a.descriptor.codigo), valor=a.descriptor.valor
                ),
            )
            for a in comunicacion.alternativas
        ],
        plazo_respuesta_horas=comunicacion.plazo_respuesta_horas,
    )


def decision_dto(
    decision: Decision, rol: RolActor, comunicacion: Comunicacion | None = None
) -> DecisionSuscripcion:
    """La decisión para el actor: solo SUSCRIPTOR ve el valor de las reglas RESERVADA."""
    insumos, perfil = decision.insumos, decision.insumos.perfil
    reglas = []
    for evaluada in decision.evaluacion.reglas:
        regla = evaluada.regla
        reservada = regla.visibilidad is Visibilidad.RESERVADA and rol is not RolActor.SUSCRIPTOR
        reglas.append(
            ReglaEvaluadaDto(
                codigo=regla.codigo,
                orden=regla.orden,
                tipo=regla.tipo,
                descripcion=regla.descripcion,
                campo=CampoRegla(regla.campo),
                operador=OperadorRegla(regla.operador),
                valor=regla.valor if isinstance(regla.valor, str) else list(regla.valor),
                valor_caso=None if reservada else evaluada.valor_caso,
                resultado=evaluada.resultado,
                visibilidad=regla.visibilidad,
                reservada=reservada,
            )
        )
    return DecisionSuscripcion(
        id=decision.id,
        codigo=decision.codigo,
        cotizacion_id=decision.cotizacion_id,
        oferta_id=decision.oferta_id,
        cotizacion_codigo=decision.cotizacion_codigo,
        cliente_id=decision.cliente_id,
        producto_id=decision.producto_id,
        resultado=decision.resultado,
        tipo=decision.tipo,
        reglas_evaluadas=reglas,
        regla_determinante=decision.evaluacion.regla_determinante,
        motivos=[MotivoCliente(m) for m in decision.evaluacion.motivos],
        version_reglas=decision.version_reglas,
        hash_reglas=decision.hash_reglas,
        condiciones=CondicionesOfertaDto(
            socio_id=insumos.socio_id,
            version_producto=insumos.version_producto,
            moneda=insumos.moneda,
            periodicidad=insumos.periodicidad,
            prima_total=texto(insumos.prima_total),
            version_reglas_prima=insumos.version_reglas_prima,
            huella_calculo=insumos.huella_calculo,
            coberturas=[
                CoberturaProductoDto(codigo=c.codigo, nombre=c.nombre, obligatoria=c.obligatoria)
                for c in insumos.coberturas
            ],
            suma_asegurada=texto(insumos.suma_asegurada),
            plazo_credito_meses=insumos.plazo_credito_meses,
            calculado_en=insumos.calculado_en,
            vigente_hasta=insumos.vigente_hasta,
        ),
        contexto=ContextoCasoDto(
            nivel_riesgo=perfil.nivel_riesgo if perfil else None,
            confianza=texto(perfil.confianza) if perfil else None,
            fuentes_consultadas=perfil.fuentes_consultadas if perfil else None,
            fuentes_declaradas=perfil.fuentes_declaradas if perfil else None,
        ),
        admite_excepcion=False,
        decidida_en=decision.decidida_en,
        duracion_ms=decision.duracion_ms,
        comunicacion=None if comunicacion is None else comunicacion_dto(comunicacion),
    )


def resumen_dto(decision: Decision) -> ResumenDecision:
    return ResumenDecision(
        id=decision.id,
        codigo=decision.codigo,
        cotizacion_id=decision.cotizacion_id,
        resultado=decision.resultado,
        version_reglas=decision.version_reglas,
        decidida_en=decision.decidida_en,
    )


def caso_dto(caso: Caso) -> CasoBandeja:
    return CasoBandeja(
        decision_id=caso.decision_id,
        codigo=caso.codigo,
        cotizacion_id=caso.cotizacion_id,
        cotizacion_codigo=caso.cotizacion_codigo,
        cliente_id=caso.cliente_id,
        producto_id=caso.producto_id,
        moneda=caso.moneda,
        suma_asegurada=texto(caso.suma_asegurada),
        motivos=[MotivoCliente(m) for m in caso.motivos],
        reglas_no_cumplidas=list(caso.reglas_no_cumplidas),
        decidida_en=caso.decidida_en,
        antiguedad_minutos=caso.antiguedad_minutos,
    )


# --- Errores ---------------------------------------------------------------------------------


def error_suscripcion(error: Exception) -> ErrorNegocio:
    """Traduce los errores de dominio a respuestas B2 estables, sin datos del caso."""
    match error:
        case CotizacionNoEncontrada():
            return ErrorNegocio("COTIZACION_NO_ENCONTRADA", "La cotización no existe", 404)
        case DecisionNoEncontrada():
            return ErrorNegocio("DECISION_NO_ENCONTRADA", "La decisión no existe", 404)
        case IdentidadNoVerificada():
            return ErrorNegocio(
                "IDENTIDAD_NO_VERIFICADA", "La identidad del cliente no está verificada", 403
            )
        case IdentidadPendiente():
            return ErrorNegocio(
                "IDENTIDAD_PENDIENTE", "La verificación de identidad no ha terminado", 409
            )
        case OfertaVencida():
            return ErrorNegocio(
                "OFERTA_VENCIDA",
                "La oferta ya no está vigente",
                409,
                {"accion": "NUEVA_COTIZACION"},
            )
        case ReglasNoVigentes():
            return ErrorNegocio(
                "REGLAS_NO_VIGENTES", "El producto no tiene reglas de suscripción vigentes", 422
            )
        case ConflictoIdempotencia():
            return ErrorNegocio(
                "IDEMPOTENCIA_CONFLICTO",
                "La clave de idempotencia ya se usó con otra solicitud",
                422,
            )
        case CursorInvalido():
            return ErrorNegocio(
                "SOLICITUD_INVALIDA", "Solicitud inválida", 422, {"campos": ["cursor"]}
            )
        case RangoInvalido():
            return ErrorNegocio(
                "SOLICITUD_INVALIDA", "Solicitud inválida", 422, {"campos": ["desde", "hasta"]}
            )
        case DependenciaNoDisponible(dependencia=dependencia):
            return ErrorNegocio(
                "DEPENDENCIA_NO_DISPONIBLE",
                "Una dependencia no está disponible",
                503,
                {"dependencia": dependencia},
            )
    raise error


ERRORES_TRADUCIBLES = (
    ConflictoIdempotencia,
    CotizacionNoEncontrada,
    CursorInvalido,
    DecisionNoEncontrada,
    DependenciaNoDisponible,
    IdentidadNoVerificada,
    IdentidadPendiente,
    OfertaVencida,
    RangoInvalido,
    ReglasNoVigentes,
)


# --- Actor y rutas ---------------------------------------------------------------------------


def actor_polizas(
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_rol: Annotated[str | None, Header(alias="X-Actor-Rol")] = None,
) -> Actor:
    """Actor de la solicitud. Se resuelve antes de validar el cuerpo: 401/403 van primero."""
    if not x_actor_id or not x_actor_rol:
        raise ErrorNegocio("ACTOR_REQUERIDO", "Faltan los encabezados del actor", 401)
    if x_actor_rol not in RolActor.__members__:
        raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
    return Actor(x_actor_id, RolActor(x_actor_rol))


def _exigir(actor: Actor, rol: RolActor) -> None:
    if actor.rol is not rol:
        raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/suscripciones",
    tags=["suscripciones"],
    dependencies=[Depends(actor_polizas)],
    responses={
        401: _error("ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
        403: _error("ROL_NO_AUTORIZADO: X-Actor-Rol no permite la operación"),
    },
)

ActorPolizas = Annotated[Actor, Depends(actor_polizas)]
IdDecision = Annotated[UUID, Path(alias="decisionId")]


def _caso(request: Request, nombre: str) -> Any:
    """El caso de uso de app.state; sin los puertos de la suscripción, 503 (no 500)."""
    caso = getattr(request.app.state, nombre, None)
    if caso is None:
        raise error_suscripcion(DependenciaNoDisponible("polizas.configuracion"))
    return caso


def _solicitar(request: Request) -> SolicitarSuscripcion:
    caso: SolicitarSuscripcion = _caso(request, "solicitar_suscripcion")
    return caso


def _consultar(request: Request) -> ConsultarSuscripciones:
    caso: ConsultarSuscripciones = _caso(request, "consultar_suscripciones")
    return caso


def _reconstruir(request: Request) -> ReconstruirDecision:
    caso: ReconstruirDecision = _caso(request, "reconstruir_decision")
    return caso


def _comunicar(request: Request) -> ComunicarDecision:
    caso: ComunicarDecision = _caso(request, "comunicar_decision")
    return caso


Consultar = Annotated[ConsultarSuscripciones, Depends(_consultar)]
Comunicar = Annotated[ComunicarDecision, Depends(_comunicar)]


@router.post(
    "",
    status_code=201,
    response_model=DecisionSuscripcion,
    responses={
        200: {
            "model": DecisionSuscripcion,
            "description": "Misma Idempotency-Key, o la cotización ya tenía una decisión (DS2-05)",
        },
        403: _error("ROL_NO_AUTORIZADO (rol distinto de ASESOR) o IDENTIDAD_NO_VERIFICADA"),
        404: _error("COTIZACION_NO_ENCONTRADA"),
        409: _error(
            "OFERTA_VENCIDA (detalles.accion = NUEVA_COTIZACION) o IDENTIDAD_PENDIENTE; "
            "no se crea decisión"
        ),
        422: _error(
            "SOLICITUD_INVALIDA, IDEMPOTENCIA_CONFLICTO o REGLAS_NO_VIGENTES (el producto no "
            "tiene reglas de suscripción vigentes)"
        ),
        503: _error("DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia: cotizacion o catalogo)"),
    },
)
async def solicitar(
    solicitud: SolicitudSuscripcion,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    respuesta: Response,
    actor: ActorPolizas,
    solicitar: Annotated[SolicitarSuscripcion, Depends(_solicitar)],
    comunicar: Comunicar,
) -> DecisionSuscripcion:
    """Evalúa las reglas vigentes sobre la oferta y registra la decisión, con su comunicación."""
    # HU-W14: la comunicación viene en la misma respuesta; el portal no hace una segunda llamada.
    _exigir(actor, RolActor.ASESOR)
    comando = ComandoSuscripcion(
        cotizacion_id=solicitud.cotizacion_id,
        idempotency_key=idempotency_key,
        actor=actor,
        correlation_id=correlation.obtener() or "",
    )
    try:
        decision, creada = await solicitar.ejecutar(comando)
    except ERRORES_TRADUCIBLES as error:
        raise error_suscripcion(error) from error
    if not creada:
        respuesta.status_code = 200
    return decision_dto(decision, actor.rol, await comunicar.ejecutar(decision))


@router.get(
    "",
    response_model=ListaDecisiones,
    responses={422: _error("SOLICITUD_INVALIDA")},
)
async def buscar_por_cotizacion(
    cotizacion_id: Annotated[UUID, Query(alias="cotizacionId")],
    consultar: Consultar,
) -> ListaDecisiones:
    """La decisión de una cotización, si existe (ASESOR o SUSCRIPTOR)."""
    decision = await consultar.por_cotizacion(cotizacion_id)
    return ListaDecisiones(items=[resumen_dto(decision)] if decision else [])


@router.get(
    "/bandeja",
    response_model=BandejaSuscripcion,
    responses={422: _error("SOLICITUD_INVALIDA")},
)
async def bandeja(
    actor: ActorPolizas,
    consultar: Consultar,
    producto_id: Annotated[str | None, Query(alias="productoId", pattern=PATRON_PRODUCTO)] = None,
    motivo: MotivoCliente | None = None,
    q: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limite: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> BandejaSuscripcion:
    """Casos REVISION_REQUERIDA del más antiguo al más reciente (solo SUSCRIPTOR). `q` busca por
    código de decisión o por el inicio del identificador de la cotización."""
    _exigir(actor, RolActor.SUSCRIPTOR)
    filtros = FiltrosBandeja(producto_id, motivo.value if motivo else None, q)
    try:
        pagina = await consultar.bandeja(filtros, limite, cursor)
    except CursorInvalido as error:
        raise error_suscripcion(error) from error
    return BandejaSuscripcion(
        items=[caso_dto(c) for c in pagina.casos],
        siguiente_cursor=pagina.siguiente_cursor,
        total=pagina.total,
    )


@router.get(
    "/resumen",
    response_model=ResumenBandeja,
    responses={422: _error("SOLICITUD_INVALIDA (también si desde ≥ hasta)")},
)
async def resumen(
    actor: ActorPolizas,
    consultar: Consultar,
    desde: Annotated[AwareDatetime, Query(description="Inicio (incluido), ISO-8601 con zona")],
    hasta: Annotated[AwareDatetime, Query(description="Fin (excluido), ISO-8601 con zona")],
) -> ResumenBandeja:
    """Indicadores de la bandeja (DS2-14) en [desde, hasta) (solo SUSCRIPTOR)."""
    _exigir(actor, RolActor.SUSCRIPTOR)
    try:
        r = await consultar.resumen(desde, hasta)
    except RangoInvalido as error:
        raise error_suscripcion(error) from error
    return ResumenBandeja(
        desde=r.desde,
        hasta=r.hasta,
        resueltas_automaticamente=r.resueltas_automaticamente,
        enviadas_a_revision=r.enviadas_a_revision,
        total=r.total,
        tiempo_medio_decision_ms=r.tiempo_medio_decision_ms,
        fuera_del_compromiso=r.fuera_del_compromiso,
        compromiso_revision_horas=r.compromiso_revision_horas,
    )


@router.get(
    "/{decisionId}",
    response_model=DecisionSuscripcion,
    responses={404: _error("DECISION_NO_ENCONTRADA"), 422: _error("SOLICITUD_INVALIDA")},
)
async def consultar(
    decision_id: IdDecision, actor: ActorPolizas, consultar: Consultar, comunicar: Comunicar
) -> DecisionSuscripcion:
    """Una decisión registrada (no cambia) con su comunicación (ASESOR o SUSCRIPTOR, DS2-07)."""
    # HU-W14: la comunicación se deriva al leer; una aprobada deja de admitir contratación al vencer.
    try:
        decision = await consultar.decision(decision_id)
    except DecisionNoEncontrada as error:
        raise error_suscripcion(error) from error
    return decision_dto(decision, actor.rol, await comunicar.ejecutar(decision))


@router.get(
    "/{decisionId}/reconstruccion",
    response_model=ReconstruccionDecision,
    responses={
        404: _error("DECISION_NO_ENCONTRADA"),
        422: _error("SOLICITUD_INVALIDA"),
        503: _error("DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia: catalogo)"),
    },
)
async def reconstruir(
    decision_id: IdDecision,
    reconstruir: Annotated[ReconstruirDecision, Depends(_reconstruir)],
) -> ReconstruccionDecision:
    """Reevalúa los insumos guardados con la versión de reglas guardada y compara."""
    try:
        r = await reconstruir.ejecutar(decision_id)
    except (DecisionNoEncontrada, DependenciaNoDisponible) as error:
        raise error_suscripcion(error) from error
    return ReconstruccionDecision(
        coincide=r.coincide,
        resultado_original=r.resultado_original,
        resultado_reconstruido=r.resultado_reconstruido,
        version_reglas=r.version_reglas,
        hash_reglas=r.hash_reglas,
    )
