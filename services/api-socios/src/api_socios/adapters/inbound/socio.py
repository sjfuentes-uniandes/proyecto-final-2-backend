import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio

from api_socios.adapters.inbound.dependencias import (
    autenticar_socio,
    consultar_oferta,
    cotizar_oferta,
)
from api_socios.adapters.inbound.errores import (
    ACCION_NUEVA_COTIZACION,
    OFERTA_NO_ENCONTRADA,
    OFERTA_VENCIDA,
    OPERACION_NO_DISPONIBLE,
    PERMISO_NO_ASIGNADO,
    SERVICIO_NO_DISPONIBLE,
    error,
    no_autenticado,
)
from api_socios.application.ofertas import ConsultarOferta, CotizarOferta
from api_socios.application.socios import AutenticarSocio
from api_socios.config import settings
from api_socios.domain.ofertas import (
    CotizacionNoDisponible,
    CotizacionRechazada,
    EstadoOferta,
    Oferta,
    OfertaNoEncontrada,
    OfertaVencida,
    SolicitudOferta,
)
from api_socios.domain.operaciones import permiso_requerido
from api_socios.domain.socios import CanalVenta, Estado, Permiso, Plan, Socio, permisos_de_alcances


@dataclass(frozen=True)
class SocioAutenticado:
    socio: Socio
    permisos: frozenset[Permiso]


async def socio_autenticado(
    caso: Annotated[AutenticarSocio, Depends(autenticar_socio)],
    x_partner_client_id: Annotated[str | None, Header()] = None,
    x_partner_key_id: Annotated[str | None, Header()] = None,
    x_partner_scopes: Annotated[str | None, Header()] = None,
) -> SocioAutenticado:
    socio = await asyncio.to_thread(caso.ejecutar, x_partner_client_id, x_partner_key_id)
    if socio is None:
        raise no_autenticado()
    permisos = (
        permisos_de_alcances(x_partner_scopes, settings.partners_resource_server) & socio.permisos
    )
    return SocioAutenticado(socio=socio, permisos=permisos)


Autenticado = Annotated[SocioAutenticado, Depends(socio_autenticado)]


async def autorizado(request: Request, autenticado: Autenticado) -> SocioAutenticado:
    permiso = permiso_requerido(request.method, request.scope["route"].path)
    if permiso not in autenticado.permisos:
        raise ErrorNegocio(
            PERMISO_NO_ASIGNADO,
            "El socio no tiene permiso para esta operación",
            403,
            {"permiso": permiso.value},
        )
    return autenticado


Autorizado = Annotated[SocioAutenticado, Depends(autorizado)]

router = APIRouter(
    tags=["socio"],
    responses={
        401: error("NO_AUTENTICADO: credenciales inexistentes, incorrectas o desactivadas"),
        403: error("PERMISO_NO_ASIGNADO (detalles.permiso)"),
        422: error("SOLICITUD_INVALIDA (detalles.campos)"),
    },
)


class PerfilSocio(BaseModel):
    """Datos propios del socio; nunca incluye el secreto ni la API key."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    nombre: str
    canal: CanalVenta
    plan: Plan
    permisos: list[Permiso]
    estado: Estado
    client_id: str = Field(alias="clientId")
    creado: datetime


@router.get("/socio/me", response_model=PerfilSocio, response_model_by_alias=True)
async def perfil(autenticado: Autenticado) -> PerfilSocio:
    socio = autenticado.socio
    return PerfilSocio(
        id=socio.id,
        nombre=socio.nombre,
        canal=socio.canal,
        plan=socio.plan,
        permisos=sorted(autenticado.permisos),
        estado=socio.estado,
        client_id=socio.client_id,
        creado=socio.creado,
    )


# --- HU-W04: contrato público de la oferta (v1) ---------------------------------------------
#
# DTO versionados y en lista blanca: solo lo que el socio necesita para presentar la oferta. Nunca
# el hash ni la huella de las reglas, los insumos (edad, nivel de riesgo), los factores del
# desglose ni ids internos (perfil, actor). La compatibilidad con la versión publicada la vigila
# tests/contract/test_contrato_publicado.py contra contracts/openapi/publicados/api-socios.v1.yaml.

TextoDecimal = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
CodigoCobertura = Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,64}$")]


class _V1(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DatosAseguradoV1(_V1):
    """Los límites los fijan las reglas del producto (DATOS_FUERA_DE_RANGO); aquí solo la forma."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    fecha_nacimiento: date
    plazo_credito_meses: Annotated[int, Field(ge=1, le=600)]
    suma_asegurada: Annotated[int, Field(ge=0, le=1_000_000_000_000)] = Field(
        description="Entero, en la moneda del producto"
    )


class SolicitudCotizacionV1(_V1):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    cliente_id: UUID
    producto_id: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,64}$")]
    datos_asegurado: DatosAseguradoV1
    coberturas_opcionales: Annotated[list[CodigoCobertura], Field(max_length=20)] = Field(
        default_factory=list
    )


class CoberturaOfertaV1(_V1):
    codigo: str
    nombre: str
    suma_asegurada: TextoDecimal = Field(description="Parte de la suma asegurada que cubre")


class ExclusionResumidaV1(_V1):
    codigo: str
    descripcion: str


class DatoFaltanteV1(_V1):
    """Un dato que una fuente de información no entregó (HU-W06). Nunca su valor."""

    fuente: str
    dato: str


class OfertaV1(_V1):
    """La oferta que el socio presenta al cliente (HU-W04)."""

    id: UUID = Field(description="Identificador de la oferta")
    codigo: str = Field(description="Identificador legible para el cliente: COT-AAAA-NNNNN")
    cotizacion_id: UUID = Field(description="Con él se consulta: GET /cotizaciones/{id}")
    producto_id: str
    precio: TextoDecimal = Field(description="Prima total, en `moneda` y por `periodicidad`")
    moneda: str = Field(description="ISO 4217")
    periodicidad: str
    coberturas: list[CoberturaOfertaV1]
    exclusiones_resumidas: list[ExclusionResumidaV1]
    vigente_hasta: datetime = Field(
        description="Fin de la vigencia (UTC): vigente si ahora < vigenteHasta"
    )
    estado: EstadoOferta
    version: int = Field(description="Versión del documento de la oferta")
    version_reglas: str = Field(description="Versión de las reglas de prima con que se calculó")
    creada_en: datetime
    # HU-W06: opcionales y aditivos (compatibles con v1).
    degradada: bool = Field(
        default=False,
        description="Oferta provisional: se calculó sin los datos de `faltantes` porque una regla "
        "vigente lo permite",
    )
    faltantes: list[DatoFaltanteV1] = Field(
        default_factory=list, description="Datos que no se obtuvieron; vacío si no es degradada"
    )


def oferta_v1(oferta: Oferta) -> OfertaV1:
    def texto(valor: Decimal) -> str:
        return format(valor, "f")

    return OfertaV1(
        id=oferta.id,
        codigo=oferta.codigo,
        cotizacion_id=oferta.cotizacion_id,
        producto_id=oferta.producto_id,
        precio=texto(oferta.precio),
        moneda=oferta.moneda,
        periodicidad=oferta.periodicidad,
        coberturas=[
            CoberturaOfertaV1(
                codigo=c.codigo, nombre=c.nombre, suma_asegurada=texto(c.suma_asegurada)
            )
            for c in oferta.coberturas
        ],
        exclusiones_resumidas=[
            ExclusionResumidaV1(codigo=e.codigo, descripcion=e.descripcion)
            for e in oferta.exclusiones
        ],
        vigente_hasta=oferta.vigente_hasta.astimezone(UTC),
        estado=oferta.estado,
        version=oferta.version,
        version_reglas=oferta.version_reglas,
        creada_en=oferta.creada_en.astimezone(UTC),
        degradada=oferta.degradada,
        faltantes=[DatoFaltanteV1(fuente=f.fuente, dato=f.dato) for f in oferta.faltantes],
    )


def error_oferta(causa: Exception) -> ErrorNegocio:
    """Traduce al contrato del socio: los rechazos de cotizacion con su mismo código (HU-W03)."""
    match causa:
        case CotizacionRechazada(codigo=codigo, estado_http=estado, detalles=detalles):
            return ErrorNegocio(
                codigo, "La solicitud de cotización fue rechazada", estado, detalles
            )
        case OfertaNoEncontrada():
            return ErrorNegocio(OFERTA_NO_ENCONTRADA, "La oferta no existe", 404)
        case OfertaVencida(oferta=oferta):
            return ErrorNegocio(
                OFERTA_VENCIDA,
                "La oferta ya no está vigente: solicita una nueva cotización",
                410,
                {
                    "accion": ACCION_NUEVA_COTIZACION,
                    "ofertaId": str(oferta.id),
                    "cotizacionId": str(oferta.cotizacion_id),
                    "vigenteHasta": oferta.vigente_hasta.astimezone(UTC).isoformat(),
                },
            )
        case _:
            return ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Servicio no disponible", 503)


ERRORES_OFERTA = (CotizacionRechazada, OfertaNoEncontrada, OfertaVencida, CotizacionNoDisponible)
_VENCIDA = error(
    "OFERTA_VENCIDA: la oferta ya no está vigente; detalles.accion = NUEVA_COTIZACION "
    "(con ofertaId, cotizacionId y vigenteHasta)"
)
_NO_DISPONIBLE = error("SERVICIO_NO_DISPONIBLE: la cotización no respondió (sin detalles)")


@router.post(
    "/cotizaciones",
    status_code=201,
    response_model=OfertaV1,
    responses={
        200: {"model": OfertaV1, "description": "Misma Idempotency-Key y mismo cuerpo"},
        # Como en /admin/socios: un JSON mal formado es 422; bytes que no se pueden leer, 400.
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        404: error("CLIENTE_NO_ENCONTRADO"),
        410: _VENCIDA,
        422: error(
            "SOLICITUD_INVALIDA, IDEMPOTENCIA_CONFLICTO, PRODUCTO_NO_ENCONTRADO, "
            "PRODUCTO_INACTIVO, PRODUCTO_FUERA_DE_VIGENCIA, REGLAS_NO_VIGENTES, "
            "DATOS_FUERA_DE_RANGO o COBERTURA_NO_DISPONIBLE (detalles.campos, sin valores)"
        ),
        503: _NO_DISPONIBLE,
    },
)
async def solicitar_cotizacion(
    autenticado: Autorizado,
    solicitud: SolicitudCotizacionV1,
    respuesta: Response,
    caso: Annotated[CotizarOferta, Depends(cotizar_oferta)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> OfertaV1:
    """Calcula la prima con el producto y las reglas vigentes y devuelve la oferta (HU-W04)."""
    datos = solicitud.datos_asegurado
    try:
        oferta, creada = await caso.ejecutar(
            autenticado.socio.id,
            SolicitudOferta(
                cliente_id=solicitud.cliente_id,
                producto_id=solicitud.producto_id,
                fecha_nacimiento=datos.fecha_nacimiento,
                plazo_credito_meses=datos.plazo_credito_meses,
                suma_asegurada=datos.suma_asegurada,
                coberturas_opcionales=tuple(solicitud.coberturas_opcionales),
            ),
            idempotency_key,
        )
    except ERRORES_OFERTA as causa:
        raise error_oferta(causa) from causa
    if not creada:
        respuesta.status_code = 200
    return oferta_v1(oferta)


@router.get(
    "/cotizaciones/{cotizacion_id}",
    response_model=OfertaV1,
    responses={
        404: error("OFERTA_NO_ENCONTRADA (también si es de otro socio: no revela que existe)"),
        410: _VENCIDA,
        503: _NO_DISPONIBLE,
    },
)
async def consultar_cotizacion(
    autenticado: Autorizado,
    caso: Annotated[ConsultarOferta, Depends(consultar_oferta)],
    cotizacion_id: Annotated[UUID, Path()],
) -> OfertaV1:
    """La oferta vigente de una cotización del socio; vencida, 410 OFERTA_VENCIDA (AC3)."""
    try:
        return oferta_v1(await caso.ejecutar(autenticado.socio.id, cotizacion_id))
    except ERRORES_OFERTA as causa:
        raise error_oferta(causa) from causa


def _no_disponible() -> ErrorNegocio:
    return ErrorNegocio(OPERACION_NO_DISPONIBLE, "Operación aún no disponible para socios", 501)


# Pendientes de otras historias: al reenviarlas, filtrar por autenticado.socio.id.


@router.post("/polizas", status_code=201, responses={501: error("OPERACION_NO_DISPONIBLE")})
async def emitir_poliza(autenticado: Autorizado) -> None:
    raise _no_disponible()


@router.get("/polizas/{poliza_id}", responses={501: error("OPERACION_NO_DISPONIBLE")})
async def consultar_poliza(
    autenticado: Autorizado, poliza_id: Annotated[str, Path(max_length=64)]
) -> None:
    raise _no_disponible()


@router.post("/siniestros", status_code=201, responses={501: error("OPERACION_NO_DISPONIBLE")})
async def avisar_siniestro(autenticado: Autorizado) -> None:
    raise _no_disponible()
