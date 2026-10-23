"""API interna de la cotización (HU-W03): /v1/cotizaciones y /v1/productos/{id}/condiciones.

La consumen bff-web (portal del asesor) y, desde HU-W04, api-socios (el contrato público del socio
es de HU-W04). Actores: `ASESOR` (X-Actor-Id = sub del back-office) y `SOCIO` (X-Actor-Id = id del
socio, SOC-NNNNNN). Un socio solo cotiza para sí, sin perfiles, y solo lee sus cotizaciones.

Los montos viajan como texto decimal (exactos, nunca float). La respuesta no incluye el hash de las
reglas (minimización, como el perfil): sí su versión y la huella del cálculo.
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
    PerfilNoCorresponde,
    ProductoFueraDeVigencia,
    ProductoInactivo,
    ProductoNoEncontrado,
    ReglasNoVigentes,
    RolNoPermitido,
)
from cotizacion.domain.cotizacion.modelos import Actor, Cotizacion, EstadoCotizacion, RolActor
from cotizacion.domain.cotizacion.prima import desglose_canonico, entradas_canonicas, texto
from cotizacion.domain.cotizacion.vigencia import VIGENCIA_HORAS_POR_OMISION, vigente_hasta
from cotizacion.domain.perfilamiento.errores import (
    ClienteNoEncontrado,
    ConflictoIdempotencia,
    DependenciaNoDisponible,
    PerfilNoEncontrado,
)
from cotizacion.domain.perfilamiento.modelos import NivelRiesgo

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


def respuesta_de(
    cotizacion: Cotizacion,
    presupuesto_ms: int,
    pasos: tuple[Paso, ...] = (),
    *,
    vigencia_horas: int = VIGENCIA_HORAS_POR_OMISION,
) -> CotizacionRespuesta:
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
        vigente_hasta=vigente_hasta(cotizacion.calculado_en, vigencia_horas).astimezone(UTC),
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
        503: _error("DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia)"),
    },
)
async def cotizar(
    solicitud: SolicitudCotizacion,
    respuesta: Response,
    actor: ActorCotizacion,
    calcular: Annotated[CalcularPrima, Depends(_calcular)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
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
    )
    try:
        cotizacion, pasos, creada = await calcular.ejecutar(comando)
    except ERRORES_TRADUCIBLES as error:
        raise error_cotizacion(error) from error
    if not creada:
        respuesta.status_code = 200
    config = calcular.config
    return respuesta_de(
        cotizacion, config.presupuesto_ms, pasos, vigencia_horas=config.oferta_vigencia_horas
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
    """Una cotización calculada; las cotizaciones no cambian nunca."""
    try:
        cotizacion = await consultar.ejecutar(cotizacion_id, actor)
    except CotizacionNoEncontrada as error:
        raise error_cotizacion(error) from error
    config = calcular.config
    return respuesta_de(
        cotizacion, config.presupuesto_ms, vigencia_horas=config.oferta_vigencia_horas
    )


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
