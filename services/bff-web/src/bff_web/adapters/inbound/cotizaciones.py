"""HU-W03: cálculo de la prima desde el portal (mockups "Nueva cotización", "Revisa los datos" y
"Calculando tu oferta"). Solo el grupo asesores.

Los DTO son copia de los de cotizacion (contracts/openapi/cotizacion.yaml) con los mismos
nombres: tests/contract/test_openapi.py verifica que sus esquemas sean iguales. El asesor es el
actor (X-Authenticated-Sub); el canal de venta del socio, si lo hay, va en `socioId`.
"""

from datetime import date, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.perfilamiento import NivelRiesgo, actor
from bff_web.adapters.inbound.socios import ErrorDto
from bff_web.adapters.outbound.cotizacion import CotizacionCliente
from bff_web.application.cotizaciones import Cotizaciones
from bff_web.config import settings


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


_RESPUESTAS: dict[int | str, dict[str, Any]] = {
    401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
    403: _error("ROL_NO_AUTORIZADO: falta el grupo asesores o no es del back-office"),
    422: _error("SOLICITUD_INVALIDA (detalles.campos)"),
    503: _error("SERVICIO_NO_DISPONIBLE o DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia)"),
}
router = APIRouter(
    prefix="/v1/cotizaciones",
    tags=["cotizaciones"],
    route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_asesores))],
    responses=_RESPUESTAS,
)
router_productos = APIRouter(
    prefix="/v1/productos",
    tags=["cotizaciones"],
    route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_asesores))],
    responses=_RESPUESTAS,
)


# --- Enumeraciones y DTO (copia de cotizacion) ----------------------------------------------

TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
CodigoCobertura = Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,64}$")]


class EstadoCotizacion(StrEnum):
    CALCULADA = "CALCULADA"


class OrigenDato(StrEnum):
    """De dónde salió el producto o las reglas (paso "Catálogo y reglas" del portal)."""

    CACHE = "CACHE"
    MEMORIA = "MEMORIA"
    CATALOGO = "CATALOGO"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DatosAseguradoDto(_Dto):
    """Los límites del producto los aplican sus reglas (DATOS_FUERA_DE_RANGO); aquí solo la forma."""

    fecha_nacimiento: date
    plazo_credito_meses: Annotated[int, Field(ge=1, le=600)]
    suma_asegurada: Annotated[int, Field(ge=0, le=1_000_000_000_000)]


class SolicitudCotizacion(_Dto):
    cliente_id: UUID
    producto_id: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,64}$")]
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
    # HU-W13 (DS2-20): aditivo y opcional, copia del campo de cotizacion.
    vigente_hasta: datetime | None = Field(
        default=None,
        description=(
            "Fin de la vigencia de la oferta (UTC): vigente si ahora < vigenteHasta (DS2-20, HU-W13)"
        ),
    )


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


# --- Dependencias y rutas -------------------------------------------------------------------


@lru_cache
def cotizaciones() -> Cotizaciones:
    return Cotizaciones(CotizacionCliente(settings.cotizacion_url, settings.cotizacion_timeout_ms))


Servicio = Annotated[Cotizaciones, Depends(cotizaciones)]
Actor = Annotated[str, Depends(actor)]


@router.post(
    "",
    response_model=CotizacionRespuesta,
    response_model_exclude_unset=True,
    status_code=201,
    responses={
        200: {"model": CotizacionRespuesta, "description": "Misma Idempotency-Key y mismo cuerpo"},
        404: _error("CLIENTE_NO_ENCONTRADO o PERFIL_NO_ENCONTRADO"),
        422: _error(
            "SOLICITUD_INVALIDA, IDEMPOTENCIA_CONFLICTO, PRODUCTO_NO_ENCONTRADO, "
            "PRODUCTO_INACTIVO, PRODUCTO_FUERA_DE_VIGENCIA, REGLAS_NO_VIGENTES, "
            "DATOS_FUERA_DE_RANGO, COBERTURA_NO_DISPONIBLE o PERFIL_NO_CORRESPONDE "
            "(detalles.campos, sin valores)"
        ),
    },
)
async def cotizar(
    solicitud: SolicitudCotizacion,
    http: Response,
    servicio: Servicio,
    asesor: Actor,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> dict[str, Any]:
    """Calcula la prima con el producto y las reglas vigentes (paso 3, "Calculando tu oferta")."""
    estado, cotizacion = await servicio.cotizar(
        solicitud.model_dump(mode="json", by_alias=True, exclude_none=True),
        asesor,
        idempotency_key,
    )
    http.status_code = estado
    return cotizacion


@router.get(
    "/{cotizacionId}",
    response_model=CotizacionRespuesta,
    response_model_exclude_unset=True,
    responses={404: _error("COTIZACION_NO_ENCONTRADA")},
)
async def consultar_cotizacion(
    cotizacion_id: Annotated[UUID, Path(alias="cotizacionId")], servicio: Servicio, asesor: Actor
) -> dict[str, Any]:
    """Una cotización calculada; no cambia nunca."""
    return await servicio.obtener(cotizacion_id, asesor)


@router_productos.get(
    "/{productoId}/condiciones",
    response_model=CondicionesProducto,
    responses={
        404: _error("PRODUCTO_NO_ENCONTRADO"),
        422: _error(
            "SOLICITUD_INVALIDA, PRODUCTO_INACTIVO, PRODUCTO_FUERA_DE_VIGENCIA o REGLAS_NO_VIGENTES"
        ),
    },
)
async def condiciones_producto(
    producto_id: Annotated[str, Path(alias="productoId", pattern=r"^[a-z0-9-]{1,64}$")],
    servicio: Servicio,
    asesor: Actor,
) -> dict[str, Any]:
    """Coberturas, versiones y límites del producto vigente (mockups 1 y 2)."""
    return await servicio.condiciones(producto_id, asesor)
