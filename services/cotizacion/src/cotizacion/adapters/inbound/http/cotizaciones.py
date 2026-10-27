"""API interna de la cotización (HU-W03): /v1/cotizaciones y /v1/productos/{id}/condiciones.

La consumen bff-web (portal del asesor) y, desde HU-W04, api-socios (el contrato público del socio
es de HU-W04). Actores: `ASESOR` (X-Actor-Id = sub del back-office) y `SOCIO` (X-Actor-Id = id del
socio, SOC-NNNNNN). Un socio solo cotiza para sí, sin perfiles, y solo lee sus cotizaciones.

Los montos viajan como texto decimal (exactos, nunca float). La respuesta no incluye el hash de las
reglas (minimización, como el perfil): sí su versión y la huella del cálculo.

HU-W04: la respuesta trae la oferta (`oferta`, aditivo) con su estado al momento de la consulta.
Una oferta vencida se sigue leyendo aquí (200 con `estado: VENCIDA`, para el portal); el 410 al
socio lo decide api-socios.

HU-W06 (aditivo): `degradada`, `faltantes` (fuente, dato, motivo y la regla aplicada) y
`versionReglasFallback`; nunca el valor de un dato faltante. Una fuente sin fallback permitido
responde 503 DEPENDENCIA_NO_DISPONIBLE con la decisión en `detalles`. La decisión de una cotización
se lee en GET /v1/cotizaciones/{id}/decision.
"""

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common import correlation
from solventa_common.errors import ROL_NO_AUTORIZADO, ErrorNegocio

from cotizacion.adapters.inbound.http.perfilamiento import ErrorDto
from cotizacion.application.cotizacion.calcular_prima import (
    CalcularPrima,
    ComandoCotizacion,
    Paso,
)
from cotizacion.application.cotizacion.consultar import (
    Condiciones,
    ConsultarCondiciones,
    ConsultarCotizacion,
)
from cotizacion.application.cotizacion.puertos import OrigenDato
from cotizacion.domain.cotizacion.errores import (
    CoberturaNoDisponible,
    CotizacionNoEncontrada,
    DatosFueraDeRango,
    OfertaVencida,
    PerfilNoCorresponde,
    ProductoFueraDeVigencia,
    ProductoInactivo,
    ProductoNoEncontrado,
    ReglasNoVigentes,
    RolNoPermitido,
)
from cotizacion.domain.cotizacion.fallback import (
    AccionSugerida,
    CotizacionPendiente,
    DecisionRegistrada,
    Faltante,
    MensajeFallback,
    MotivoFalla,
)
from cotizacion.domain.cotizacion.modelos import (
    Actor,
    Cotizacion,
    EstadoCotizacion,
    EstadoOferta,
    Oferta,
    RolActor,
)
from cotizacion.domain.cotizacion.oferta import vigente_hasta_de
from cotizacion.domain.cotizacion.prima import desglose_canonico, entradas_canonicas, texto
from cotizacion.domain.cotizacion.vigencia import VIGENCIA_HORAS_POR_OMISION
from cotizacion.domain.perfilamiento.errores import (
    ClienteNoEncontrado,
    ConflictoIdempotencia,
    DependenciaNoDisponible,
    PerfilNoEncontrado,
)
from cotizacion.domain.perfilamiento.modelos import NivelRiesgo

ACCION_OFERTA_VENCIDA = "NUEVA_COTIZACION"

PATRON_SOCIO = re.compile(r"SOC-\d{6}")
PATRON_PRODUCTO = r"^[a-z0-9-]{1,64}$"


def actor_cotizacion(
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_rol: Annotated[str | None, Header(alias="X-Actor-Rol")] = None,
) -> Actor:
    """Actor de la solicitud. Se resuelve antes de validar el cuerpo: 401/403 van primero."""
    if not x_actor_id or not x_actor_rol:
        raise ErrorNegocio("ACTOR_REQUERIDO", "Faltan los encabezados del actor", 401)
    if x_actor_rol not in RolActor.__members__:
        raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
    rol = RolActor(x_actor_rol)
    if rol is RolActor.SOCIO and not PATRON_SOCIO.fullmatch(x_actor_id):
        raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
    return Actor(x_actor_id, rol)


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


_ERRORES_ACTOR: dict[int | str, dict[str, Any]] = {
    401: _error("ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
    403: _error("ROL_NO_AUTORIZADO: X-Actor-Rol distinto de ASESOR o SOCIO (SOC-NNNNNN)"),
}

router = APIRouter(
    prefix="/v1/cotizaciones",
    tags=["cotizaciones"],
    dependencies=[Depends(actor_cotizacion)],
    responses=_ERRORES_ACTOR,
)
router_productos = APIRouter(
    prefix="/v1/productos",
    tags=["cotizaciones"],
    dependencies=[Depends(actor_cotizacion)],
    responses=_ERRORES_ACTOR,
)

# --- DTO ---------------------------------------------------------------------------------

TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
DESCRIPCION_VIGENTE_HASTA = (
    "Fin de la vigencia de la oferta (UTC): vigente si ahora < vigenteHasta (DS2-20, HU-W13)"
)
CodigoCobertura = Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,64}$")]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DatosAseguradoDto(_Dto):
    """Los límites del producto los aplican sus reglas (DATOS_FUERA_DE_RANGO); aquí solo la forma."""

    fecha_nacimiento: date
    plazo_credito_meses: Annotated[int, Field(ge=1, le=600)]
    suma_asegurada: Annotated[int, Field(ge=0, le=1_000_000_000_000)]


class SolicitudCotizacion(_Dto):
    cliente_id: UUID
    producto_id: Annotated[str, Field(pattern=PATRON_PRODUCTO)]
    datos_asegurado: DatosAseguradoDto
    coberturas_opcionales: Annotated[list[CodigoCobertura], Field(max_length=20)] = Field(
        default_factory=list
    )
    perfil_id: UUID | None = Field(
        default=None, description="Perfil de riesgo que sustenta la cotización (0..1); solo ASESOR"
    )
    socio_id: Annotated[str, Field(pattern=r"^SOC-\d{6}$")] | None = Field(
        default=None,
        description="Canal de venta (0..1). Con X-Actor-Rol SOCIO es el propio socio",
    )


class CoberturaProductoDto(_Dto):
    codigo: str
    nombre: str
    obligatoria: bool


class FactorPrimaDto(_Dto):
    codigo: str
    valor: str
    factor: TextoDecimal


class RecargoPrimaDto(_Dto):
    cobertura: str
    tasa: TextoDecimal
    valor: TextoDecimal


class DesglosePrimaDto(_Dto):
    prima_base: TextoDecimal
    factores: list[FactorPrimaDto]
    prima_ajustada: TextoDecimal
    recargos: list[RecargoPrimaDto]
    subtotal: TextoDecimal
    ajuste_limite: TextoDecimal
    prima_total: TextoDecimal


class EntradasPrimaDto(_Dto):
    suma_asegurada: TextoDecimal
    plazo_credito_meses: int
    edad: int
    nivel_riesgo: NivelRiesgo | None
    coberturas_opcionales: list[str]


class PasoCotizacionDto(_Dto):
    codigo: str
    duracion_ms: int
    origen: OrigenDato | None


class CoberturaOfertaDto(_Dto):
    codigo: str
    nombre: str
    suma_asegurada: TextoDecimal


class ExclusionDto(_Dto):
    codigo: str
    descripcion: str


class OfertaDto(_Dto):
    """La oferta de la cotización (HU-W04). Inmutable; `estado` es el del instante de la consulta."""

    id: UUID
    codigo: str = Field(description="Identificador legible para el cliente: COT-AAAA-NNNNN")
    cotizacion_id: UUID
    precio: TextoDecimal = Field(description="La prima total, en `moneda` y por `periodicidad`")
    moneda: str
    periodicidad: str
    coberturas: list[CoberturaOfertaDto]
    exclusiones_resumidas: list[ExclusionDto]
    version: int = Field(description="Versión del documento de la oferta")
    version_reglas: str
    creada_en: datetime
    vigente_hasta: datetime = Field(description=DESCRIPCION_VIGENTE_HASTA)
    estado: EstadoOferta


class ReglaAplicadaDto(_Dto):
    id: str
    version: str


class FaltanteDto(_Dto):
    """Un dato que no se obtuvo (HU-W06). Solo su nombre: el valor no existe y no se inventa."""

    fuente: str
    dato: str
    motivo: MotivoFalla
    regla: ReglaAplicadaDto | None = Field(
        description="Regla de fallback aplicada; null si la fuente no tenía regla vigente"
    )


class CotizacionRespuesta(_Dto):
    id: UUID
    cliente_id: UUID
    socio_id: str | None
    producto_id: str
    version_producto: str
    moneda: str
    periodicidad: str
    version_reglas: str
    perfil_id: UUID | None
    estado: EstadoCotizacion
    coberturas: list[CoberturaProductoDto]
    entradas: EntradasPrimaDto
    desglose: DesglosePrimaDto
    prima_total: TextoDecimal
    huella_calculo: str
    calculado_en: datetime
    duracion_ms: int
    presupuesto_ms: int
    pasos: list[PasoCotizacionDto] = Field(
        default_factory=list,
        description="Pasos del cálculo con su duración; vacío al leer o al repetir la clave",
    )
    # HU-W13 (DS2-20): aditivo y opcional; HU-W04 conserva su nombre y su significado.
    vigente_hasta: datetime | None = Field(default=None, description=DESCRIPCION_VIGENTE_HASTA)
    # HU-W04: aditivo y opcional; null en las cotizaciones anteriores a la migración 004.
    oferta: OfertaDto | None = Field(default=None, description="La oferta de la cotización")
    # HU-W06: aditivos; false, vacío y null en el recorrido nominal y antes de la migración 005.
    degradada: bool = Field(
        default=False,
        description="Oferta provisional: una fuente opcional faltó y una regla permitió degradar",
    )
    faltantes: list[FaltanteDto] = Field(default_factory=list)
    version_reglas_fallback: str | None = None


class DecisionCotizacionRespuesta(_Dto):
    """La decisión de fallback de una cotización (HU-W06, AC3). NOMINAL si ninguna fuente faltó."""

    cotizacion_id: UUID
    decision: str = Field(description="NOMINAL o DEGRADADA")
    decision_id: UUID | None
    perfil_id: UUID | None
    faltantes: list[FaltanteDto]
    version_reglas_fallback: str | None
    mensaje_codigo: MensajeFallback | None
    decidido_en: datetime | None


class RangoDto(_Dto):
    minimo: TextoDecimal
    maximo: TextoDecimal


class LimitesDto(_Dto):
    suma_asegurada: RangoDto
    plazo_credito_meses: RangoDto
    edad: RangoDto


class CondicionesProducto(_Dto):
    producto_id: str
    nombre: str
    ramo: str
    pais: str
    moneda: str
    version_producto: str
    version_reglas: str
    periodicidad: str
    vigente_hasta: datetime | None
    coberturas: list[CoberturaProductoDto]
    limites: LimitesDto


def oferta_de(oferta: Oferta, ahora: datetime) -> OfertaDto:
    return OfertaDto(
        id=oferta.id,
        codigo=oferta.codigo or "",
        cotizacion_id=oferta.cotizacion_id,
        precio=texto(oferta.precio),
        moneda=oferta.moneda,
        periodicidad=oferta.periodicidad,
        coberturas=[
            CoberturaOfertaDto(
                codigo=c.codigo, nombre=c.nombre, suma_asegurada=texto(c.suma_asegurada)
            )
            for c in oferta.coberturas
        ],
        exclusiones_resumidas=[
            ExclusionDto(codigo=e.codigo, descripcion=e.descripcion) for e in oferta.exclusiones
        ],
        version=oferta.version,
        version_reglas=oferta.version_reglas,
        creada_en=oferta.creada_en.astimezone(UTC),
        vigente_hasta=oferta.vigente_hasta.astimezone(UTC),
        estado=oferta.estado(ahora),
    )


def faltantes_de(faltantes: tuple[Faltante, ...]) -> list[FaltanteDto]:
    return [
        FaltanteDto(
            fuente=f.fuente,
            dato=f.dato,
            motivo=f.motivo,
            regla=None
            if f.regla is None
            else ReglaAplicadaDto(id=f.regla.id, version=f.regla.version),
        )
        for f in faltantes
    ]


def decision_respuesta(cotizacion: Cotizacion) -> DecisionCotizacionRespuesta:
    registro = cotizacion.decision
    if registro is None:
        return DecisionCotizacionRespuesta(
            cotizacion_id=cotizacion.id,
            decision="NOMINAL",
            decision_id=None,
            perfil_id=cotizacion.perfil_id,
            faltantes=[],
            version_reglas_fallback=None,
            mensaje_codigo=None,
            decidido_en=None,
        )
    decision = registro.decision
    return DecisionCotizacionRespuesta(
        cotizacion_id=cotizacion.id,
        decision=decision.tipo.value,
        decision_id=registro.id,
        perfil_id=registro.perfil_id,
        faltantes=faltantes_de(decision.faltantes),
        version_reglas_fallback=decision.version_reglas,
        mensaje_codigo=decision.mensaje_codigo,
        decidido_en=registro.decidido_en.astimezone(UTC),
    )


def detalles_pendiente(registro: DecisionRegistrada) -> dict[str, Any]:
    """Los detalles del 503 de una cotización PENDIENTE: la decisión, sin valores."""
    decision = registro.decision
    return {
        "dependencia": CotizacionPendiente(registro).dependencia,
        "decisionId": str(registro.id),
        "accionSugerida": AccionSugerida.RECALCULAR_PERFIL.value,
        "mensajeCodigo": decision.mensaje_codigo.value,
        "versionReglasFallback": decision.version_reglas,
        "faltantes": [
            f.model_dump(mode="json", by_alias=True) for f in faltantes_de(decision.faltantes)
        ],
    }


def respuesta_de(
    cotizacion: Cotizacion,
    presupuesto_ms: int,
    pasos: tuple[Paso, ...] = (),
    *,
    vigencia_horas: int = VIGENCIA_HORAS_POR_OMISION,
    ahora: datetime | None = None,
) -> CotizacionRespuesta:
    """`ahora` decide el estado de la oferta; sin él, el del reloj del sistema."""
    return CotizacionRespuesta(
        id=cotizacion.id,
        cliente_id=cotizacion.cliente_id,
        socio_id=cotizacion.socio_id,
        producto_id=cotizacion.producto_id,
        version_producto=cotizacion.version_producto,
        moneda=cotizacion.moneda,
        periodicidad=cotizacion.periodicidad,
        version_reglas=cotizacion.version_reglas,
        perfil_id=cotizacion.perfil_id,
        estado=cotizacion.estado,
        coberturas=[
            CoberturaProductoDto(codigo=c.codigo, nombre=c.nombre, obligatoria=c.obligatoria)
            for c in cotizacion.coberturas
        ],
        entradas=EntradasPrimaDto.model_validate(entradas_canonicas(cotizacion.entradas)),
        desglose=DesglosePrimaDto.model_validate(desglose_canonico(cotizacion.desglose)),
        prima_total=texto(cotizacion.desglose.prima_total),
        huella_calculo=cotizacion.huella_calculo,
        calculado_en=cotizacion.calculado_en.astimezone(UTC),
        duracion_ms=cotizacion.duracion_ms,
        presupuesto_ms=presupuesto_ms,
        pasos=[
            PasoCotizacionDto(codigo=p.codigo, duracion_ms=p.duracion_ms, origen=p.origen)
            for p in pasos
        ],
        vigente_hasta=vigente_hasta_de(cotizacion, vigencia_horas).astimezone(UTC),
        oferta=None
        if cotizacion.oferta is None
        else oferta_de(cotizacion.oferta, ahora or datetime.now(UTC)),
        degradada=cotizacion.degradada,
        faltantes=faltantes_de(cotizacion.decision.decision.faltantes)
        if cotizacion.decision
        else [],
        version_reglas_fallback=cotizacion.decision.decision.version_reglas
        if cotizacion.decision
        else None,
    )


def condiciones_de(condiciones: Condiciones) -> CondicionesProducto:
    producto, reglas = condiciones.producto, condiciones.reglas
    limites = reglas.limites
    return CondicionesProducto(
        producto_id=producto.id,
        nombre=producto.nombre,
        ramo=producto.ramo,
        pais=producto.pais,
        moneda=producto.moneda,
        version_producto=producto.version,
        version_reglas=reglas.version,
        periodicidad=reglas.periodicidad,
        vigente_hasta=producto.vigente_hasta,
        coberturas=[
            CoberturaProductoDto(codigo=c.codigo, nombre=c.nombre, obligatoria=c.obligatoria)
            for c in producto.coberturas
        ],
        limites=LimitesDto(
            suma_asegurada=RangoDto(
                minimo=texto(limites.suma_asegurada.minimo),
                maximo=texto(limites.suma_asegurada.maximo),
            ),
            plazo_credito_meses=RangoDto(
                minimo=texto(limites.plazo_credito_meses.minimo),
                maximo=texto(limites.plazo_credito_meses.maximo),
            ),
            edad=RangoDto(minimo=texto(limites.edad.minimo), maximo=texto(limites.edad.maximo)),
        ),
    )


# --- Errores -------------------------------------------------------------------------------


def error_cotizacion(error: Exception, producto_en_ruta: bool = False) -> ErrorNegocio:
    """Traduce los errores de dominio a respuestas B2 estables, sin datos del asegurado.

    PRODUCTO_NO_ENCONTRADO es 422 al cotizar (el producto es un campo del cuerpo) y 404 en
    /v1/productos/{productoId}/condiciones (el producto es el recurso de la ruta).
    """
    match error:
        case ProductoNoEncontrado():
            estado = 404 if producto_en_ruta else 422
            return ErrorNegocio("PRODUCTO_NO_ENCONTRADO", "El producto no existe", estado)
        case ProductoInactivo():
            return ErrorNegocio("PRODUCTO_INACTIVO", "El producto no está activo", 422)
        case ProductoFueraDeVigencia():
            return ErrorNegocio(
                "PRODUCTO_FUERA_DE_VIGENCIA", "El producto no está vigente en la fecha", 422
            )
        case ReglasNoVigentes():
            return ErrorNegocio(
                "REGLAS_NO_VIGENTES", "El producto no tiene reglas de prima vigentes", 422
            )
        case DatosFueraDeRango(campos=campos):
            return ErrorNegocio(
                "DATOS_FUERA_DE_RANGO",
                "Hay datos fuera de los límites del producto",
                422,
                {"campos": list(campos)},
            )
        case CoberturaNoDisponible(campos=campos):
            return ErrorNegocio(
                "COBERTURA_NO_DISPONIBLE",
                "Una cobertura no es opcional en el producto",
                422,
                {"campos": list(campos)},
            )
        case PerfilNoCorresponde(campos=campos):
            return ErrorNegocio(
                "PERFIL_NO_CORRESPONDE",
                "El perfil no corresponde a la solicitud",
                422,
                {"campos": list(campos)},
            )
        case PerfilNoEncontrado():
            return ErrorNegocio("PERFIL_NO_ENCONTRADO", "El perfil no existe", 404)
        case ClienteNoEncontrado():
            return ErrorNegocio("CLIENTE_NO_ENCONTRADO", "El cliente no existe", 404)
        case ConflictoIdempotencia():
            return ErrorNegocio(
                "IDEMPOTENCIA_CONFLICTO",
                "La clave de idempotencia ya se usó con otra solicitud",
                422,
            )
        case RolNoPermitido():
            return ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
        case OfertaVencida():
            # El mismo código y detalle que polizas (HU-W13): hay que cotizar de nuevo.
            return ErrorNegocio(
                "OFERTA_VENCIDA",
                "La oferta ya no está vigente",
                409,
                {"accion": ACCION_OFERTA_VENCIDA},
            )
        case CotizacionPendiente(registro=registro):
            # HU-W06: el mismo código recuperable de HU-W04/W05, con la decisión registrada.
            return ErrorNegocio(
                "DEPENDENCIA_NO_DISPONIBLE",
                "Falta una fuente de datos y no hay una respuesta degradada permitida",
                503,
                detalles_pendiente(registro),
            )
        case DependenciaNoDisponible(dependencia=dependencia):
            return ErrorNegocio(
                "DEPENDENCIA_NO_DISPONIBLE",
                "Una dependencia no está disponible",
                503,
                {"dependencia": dependencia},
            )
        case _:
            return ErrorNegocio("COTIZACION_NO_ENCONTRADA", "La cotización no existe", 404)


ERRORES_TRADUCIBLES = (
    ProductoNoEncontrado,
    ProductoInactivo,
    ProductoFueraDeVigencia,
    ReglasNoVigentes,
    DatosFueraDeRango,
    CoberturaNoDisponible,
    PerfilNoCorresponde,
    PerfilNoEncontrado,
    ClienteNoEncontrado,
    ConflictoIdempotencia,
    RolNoPermitido,
    DependenciaNoDisponible,
    CotizacionNoEncontrada,
    OfertaVencida,
)

# --- Rutas ---------------------------------------------------------------------------------


def _caso(request: Request, nombre: str) -> Any:
    """El caso de uso de app.state; sin los puertos de la cotización, 503 (no 500)."""
    caso = getattr(request.app.state, nombre, None)
    if caso is None:
        raise error_cotizacion(DependenciaNoDisponible("cotizacion.configuracion"))
    return caso


def _calcular(request: Request) -> CalcularPrima:
    caso: CalcularPrima = _caso(request, "calcular_prima")
    return caso


def _consultar(request: Request) -> ConsultarCotizacion:
    caso: ConsultarCotizacion = _caso(request, "consultar_cotizacion")
    return caso


def _condiciones(request: Request) -> ConsultarCondiciones:
    caso: ConsultarCondiciones = _caso(request, "consultar_condiciones")
    return caso


ActorCotizacion = Annotated[Actor, Depends(actor_cotizacion)]
# HU-W05: tiempo restante de quien llama (api-socios, bff-web); acota el presupuesto del recorrido.
DeadlineMs = Annotated[
    int | None,
    Header(
        alias="X-Deadline-Ms",
        ge=1,
        description="Milisegundos que quien llama seguirá esperando (contracts/encabezados.md)",
    ),
]


@router.post(
    "",
    status_code=201,
    response_model=CotizacionRespuesta,
    responses={
        200: {"model": CotizacionRespuesta, "description": "Misma Idempotency-Key y mismo cuerpo"},
        403: _error("ROL_NO_AUTORIZADO (también: un SOCIO con perfilId o con otro socioId)"),
        404: _error("CLIENTE_NO_ENCONTRADO o PERFIL_NO_ENCONTRADO"),
        422: _error(
            "SOLICITUD_INVALIDA, IDEMPOTENCIA_CONFLICTO, PRODUCTO_NO_ENCONTRADO, "
            "PRODUCTO_INACTIVO, PRODUCTO_FUERA_DE_VIGENCIA, REGLAS_NO_VIGENTES, "
            "DATOS_FUERA_DE_RANGO, COBERTURA_NO_DISPONIBLE o PERFIL_NO_CORRESPONDE "
            "(detalles.campos, sin valores)"
        ),
        503: _error(
            "DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia). HU-W06: con dependencia "
            "perfil.fuentes, una fuente del perfil faltó sin fallback permitido; detalles trae "
            "decisionId, faltantes, versionReglasFallback y accionSugerida RECALCULAR_PERFIL"
        ),
    },
)
async def cotizar(
    solicitud: SolicitudCotizacion,
    respuesta: Response,
    actor: ActorCotizacion,
    calcular: Annotated[CalcularPrima, Depends(_calcular)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    x_deadline_ms: DeadlineMs = None,
) -> CotizacionRespuesta:
    """Calcula la prima con el producto y las reglas vigentes y guarda la cotización."""
    datos = solicitud.datos_asegurado
    comando = ComandoCotizacion(
        cliente_id=solicitud.cliente_id,
        producto_id=solicitud.producto_id,
        fecha_nacimiento=datos.fecha_nacimiento,
        plazo_credito_meses=datos.plazo_credito_meses,
        suma_asegurada=Decimal(datos.suma_asegurada),
        coberturas_opcionales=tuple(solicitud.coberturas_opcionales),
        perfil_id=solicitud.perfil_id,
        socio_id=solicitud.socio_id,
        idempotency_key=idempotency_key,
        actor=actor,
        correlation_id=correlation.obtener() or "",
        deadline_ms=x_deadline_ms,
    )
    try:
        cotizacion, pasos, creada = await calcular.ejecutar(comando)
    except ERRORES_TRADUCIBLES as error:
        raise error_cotizacion(error) from error
    if not creada:
        respuesta.status_code = 200
    config = calcular.config
    return respuesta_de(
        cotizacion,
        config.presupuesto_ms,
        pasos,
        vigencia_horas=config.oferta_vigencia_horas,
        ahora=calcular.reloj.ahora(),
    )


@router.get(
    "/{cotizacionId}",
    response_model=CotizacionRespuesta,
    responses={
        404: _error("COTIZACION_NO_ENCONTRADA (también si es de otro socio)"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def obtener(
    actor: ActorCotizacion,
    consultar: Annotated[ConsultarCotizacion, Depends(_consultar)],
    calcular: Annotated[CalcularPrima, Depends(_calcular)],
    cotizacion_id: Annotated[UUID, Path(alias="cotizacionId")],
) -> CotizacionRespuesta:
    """Una cotización calculada; las cotizaciones no cambian nunca (el estado de su oferta sí, con
    el tiempo: se calcula al leer)."""
    try:
        cotizacion = await consultar.ejecutar(cotizacion_id, actor)
    except CotizacionNoEncontrada as error:
        raise error_cotizacion(error) from error
    config = calcular.config
    return respuesta_de(
        cotizacion,
        config.presupuesto_ms,
        vigencia_horas=config.oferta_vigencia_horas,
        ahora=calcular.reloj.ahora(),
    )


@router.get(
    "/{cotizacionId}/decision",
    response_model=DecisionCotizacionRespuesta,
    responses={
        404: _error("COTIZACION_NO_ENCONTRADA (también si es de otro socio)"),
        422: _error("SOLICITUD_INVALIDA"),
    },
)
async def decision(
    actor: ActorCotizacion,
    consultar: Annotated[ConsultarCotizacion, Depends(_consultar)],
    cotizacion_id: Annotated[UUID, Path(alias="cotizacionId")],
) -> DecisionCotizacionRespuesta:
    """La decisión de fallback de la cotización (HU-W06, AC3): las fuentes y datos faltantes, el
    motivo y la regla aplicada con su versión. Para el asesor o el socio dueño."""
    try:
        cotizacion = await consultar.ejecutar(cotizacion_id, actor)
    except CotizacionNoEncontrada as error:
        raise error_cotizacion(error) from error
    return decision_respuesta(cotizacion)


@router_productos.get(
    "/{productoId}/condiciones",
    response_model=CondicionesProducto,
    responses={
        404: _error("PRODUCTO_NO_ENCONTRADO"),
        422: _error(
            "SOLICITUD_INVALIDA, PRODUCTO_INACTIVO, PRODUCTO_FUERA_DE_VIGENCIA o REGLAS_NO_VIGENTES"
        ),
        503: _error("DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia)"),
    },
)
async def condiciones(
    condiciones: Annotated[ConsultarCondiciones, Depends(_condiciones)],
    producto_id: Annotated[str, Path(alias="productoId", pattern=PATRON_PRODUCTO)],
) -> CondicionesProducto:
    """Coberturas, versiones y límites del producto vigente, para el formulario del portal."""
    try:
        return condiciones_de(await condiciones.ejecutar(producto_id))
    except ERRORES_TRADUCIBLES as error:
        raise error_cotizacion(error, producto_en_ruta=True) from error
