"""Rutas de captura móvil: límites, borradores asociados a póliza y carga directa (HU-M01)."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel
from solventa_common.errors import RutaB2

from siniestros.adapters.inbound.http.dto import ErrorDto
from siniestros.adapters.inbound.http.siniestros import Actor, actor_siniestros
from siniestros.application.siniestro.borradores import Borradores, EvidenciaSolicitada
from siniestros.application.siniestro.errores import (
    DependenciaNoDisponible,
    PolizaNoVigente,
    RolNoAutorizado,
)
from siniestros.domain.siniestro.modelos import TipoEvidencia

router_borradores = APIRouter(
    prefix="/v1/siniestros",
    tags=["borradores"],
    route_class=RutaB2,
    responses={
        401: {"model": ErrorDto, "description": "ACTOR_REQUERIDO"},
        403: {"model": ErrorDto, "description": "ROL_NO_AUTORIZADO"},
    },
)
DESCRIPCION_MAX_CARACTERES = 2000


class Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class VigenciaDto(Dto):
    desde: date
    hasta: date


class CoberturaDto(Dto):
    codigo: str
    nombre: str
    obligatoria: bool


class CopiaPolizaDto(Dto):
    poliza_id: UUID
    numero: str
    estado: str
    producto: str
    ramo: str
    vigencia: VigenciaDto
    vigente: bool
    prima: Decimal
    suma_asegurada: Decimal
    moneda: str
    periodicidad: str
    coberturas: list[CoberturaDto]


class SolicitudBorrador(Dto):
    cliente_id: UUID
    descripcion: str = Field(min_length=1, max_length=DESCRIPCION_MAX_CARACTERES)
    copia_poliza: CopiaPolizaDto

    @field_validator("descripcion")
    @classmethod
    def _descripcion_no_vacia(cls, valor: str) -> str:
        descripcion = valor.strip()
        if not descripcion:
            raise ValueError("descripcion es obligatoria")
        return descripcion


class BorradorCreado(Dto):
    borrador_id: UUID
    poliza_id: UUID
    estado: str = "ABIERTO"


class LimitesEvidencia(Dto):
    max_evidencias: int
    formatos_foto: list[str]
    formatos_video: list[str]
    tamano_maximo_foto_bytes: int
    tamano_maximo_video_bytes: int
    duracion_maxima_video_segundos: int
    url_carga_expira_segundos: int


class SolicitudEvidencia(Dto):
    nombre_archivo: str = Field(min_length=1, max_length=255)
    tipo: Literal["FOTOGRAFIA", "VIDEO"]
    tipo_contenido: str = Field(min_length=1, max_length=128)
    tamano_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capturada_en: AwareDatetime | None = None
    duracion_segundos: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _duracion_segun_tipo(self) -> "SolicitudEvidencia":
        if (self.tipo == TipoEvidencia.VIDEO.value) != (self.duracion_segundos is not None):
            raise ValueError("duracionSegundos es obligatorio solo para videos")
        return self


class CargaEvidencia(Dto):
    metodo: str
    url: str
    campos: dict[str, str]
    expira_en: AwareDatetime


class EvidenciaAgregada(Dto):
    evidencia_id: UUID
    nombre_archivo: str
    tipo: TipoEvidencia
    tipo_contenido: str
    tamano_bytes: int
    sha256: str
    capturada_en: AwareDatetime | None
    duracion_segundos: int | None
    estado_carga: Literal["PENDIENTE", "CARGADA"]
    carga_confirmada_en: AwareDatetime | None
    carga: CargaEvidencia


class BorradorResumen(Dto):
    borrador_id: UUID
    poliza_id: UUID
    descripcion: str
    estado: Literal["ABIERTO"]
    creado_en: AwareDatetime
    cantidad_evidencias: int
    evidencias_pendientes: int


class EvidenciaBorrador(Dto):
    evidencia_id: UUID
    nombre_archivo: str
    tipo: TipoEvidencia
    tipo_contenido: str
    tamano_bytes: int
    sha256: str
    capturada_en: AwareDatetime | None
    duracion_segundos: int | None
    estado_carga: Literal["PENDIENTE", "CARGADA"]
    carga_confirmada_en: AwareDatetime | None


class ListadoBorradores(Dto):
    items: list[BorradorResumen]


class BorradorDetalle(BorradorResumen):
    evidencias: list[EvidenciaBorrador]


class EvidenciaConfirmada(Dto):
    evidencia_id: UUID
    estado_carga: Literal["CARGADA"]
    carga_confirmada_en: AwareDatetime


class CargaRenovada(Dto):
    evidencia_id: UUID
    estado_carga: Literal["PENDIENTE"]
    carga: CargaEvidencia


def _borradores(request: Request) -> Borradores:
    servicio: Borradores | None = getattr(request.app.state, "borradores", None)
    if servicio is None:
        raise DependenciaNoDisponible("base de datos")
    return servicio


def _actor_cliente(
    actor: Annotated[Actor, Depends(actor_siniestros)],
) -> Actor:
    if actor.rol != "CLIENTE":
        raise RolNoAutorizado
    return actor


Cliente = Annotated[Actor, Depends(_actor_cliente)]
BorradoresDep = Annotated[Borradores, Depends(_borradores)]
BorradorId = Annotated[UUID, Path(alias="borradorId")]
EvidenciaId = Annotated[UUID, Path(alias="evidenciaId")]


@router_borradores.get(
    "/borradores",
    response_model=ListadoBorradores,
    response_model_by_alias=True,
    responses={
        200: {"description": "Borradores abiertos del actor autenticado"},
        503: {"model": ErrorDto, "description": "DEPENDENCIA_NO_DISPONIBLE"},
    },
)
async def listar_borradores(actor: Cliente, casos: BorradoresDep) -> ListadoBorradores:
    return ListadoBorradores.model_validate(await casos.listar(actor.id))


@router_borradores.get(
    "/borradores/{borradorId}",
    response_model=BorradorDetalle,
    response_model_by_alias=True,
    responses={
        404: {"model": ErrorDto, "description": "BORRADOR_NO_ENCONTRADO"},
        503: {"model": ErrorDto, "description": "DEPENDENCIA_NO_DISPONIBLE"},
    },
)
async def consultar_borrador(
    borrador_id: BorradorId, actor: Cliente, casos: BorradoresDep
) -> BorradorDetalle:
    return BorradorDetalle.model_validate(await casos.consultar(actor.id, borrador_id))


@router_borradores.get(
    "/limites",
    response_model=LimitesEvidencia,
    response_model_by_alias=True,
    responses={503: {"model": ErrorDto, "description": "DEPENDENCIA_NO_DISPONIBLE"}},
)
async def limites(_: Cliente, casos: BorradoresDep) -> LimitesEvidencia:
    return LimitesEvidencia.model_validate(casos.limites())


@router_borradores.post(
    "/borradores",
    status_code=201,
    response_model=BorradorCreado,
    response_model_by_alias=True,
    responses={
        400: {"model": ErrorDto, "description": "SOLICITUD_INVALIDA"},
        409: {"model": ErrorDto, "description": "POLIZA_NO_VIGENTE"},
        422: {"model": ErrorDto, "description": "IDEMPOTENCIA_CONFLICTO o SOLICITUD_INVALIDA"},
        503: {"model": ErrorDto, "description": "DEPENDENCIA_NO_DISPONIBLE"},
    },
)
async def crear_borrador(
    solicitud: SolicitudBorrador,
    request: Request,
    actor: Cliente,
    casos: BorradoresDep,
    clave: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> BorradorCreado:
    copia = solicitud.copia_poliza
    if copia.estado != "ACTIVA" or not copia.vigente:
        raise PolizaNoVigente
    correlation_id = request.headers.get("X-Correlation-Id", "")
    borrador_id = await casos.crear(
        solicitud.cliente_id,
        copia.poliza_id,
        solicitud.descripcion,
        copia.model_dump(by_alias=True, mode="json"),
        clave,
        actor.id,
        correlation_id,
    )
    return BorradorCreado(borrador_id=borrador_id, poliza_id=copia.poliza_id)


@router_borradores.post(
    "/borradores/{borradorId}/evidencias",
    status_code=201,
    response_model=EvidenciaAgregada,
    response_model_by_alias=True,
    responses={
        400: {"model": ErrorDto, "description": "SOLICITUD_INVALIDA"},
        404: {"model": ErrorDto, "description": "BORRADOR_NO_ENCONTRADO"},
        422: {
            "model": ErrorDto,
            "description": (
                "TIPO_EVIDENCIA_NO_ADMITIDO, TAMANO_EVIDENCIA_EXCEDIDO, "
                "DURACION_VIDEO_EXCEDIDA o LIMITE_EVIDENCIAS_ALCANZADO"
            ),
        },
        503: {"model": ErrorDto, "description": "DEPENDENCIA_NO_DISPONIBLE"},
    },
)
async def agregar_evidencia(
    solicitud: SolicitudEvidencia,
    borrador_id: BorradorId,
    actor: Cliente,
    casos: BorradoresDep,
) -> EvidenciaAgregada:
    carga: dict[str, Any] = await casos.agregar_evidencia(
        actor.id,
        borrador_id,
        EvidenciaSolicitada(
            nombre_archivo=solicitud.nombre_archivo,
            tipo=TipoEvidencia(solicitud.tipo),
            tipo_contenido=solicitud.tipo_contenido,
            tamano_bytes=solicitud.tamano_bytes,
            sha256=solicitud.sha256,
            capturada_en=solicitud.capturada_en,
            duracion_segundos=solicitud.duracion_segundos,
        ),
    )
    return EvidenciaAgregada.model_validate(carga)


@router_borradores.post(
    "/borradores/{borradorId}/evidencias/{evidenciaId}/confirmacion",
    response_model=EvidenciaConfirmada,
    response_model_by_alias=True,
    responses={
        404: {"model": ErrorDto, "description": "BORRADOR_NO_ENCONTRADO"},
        409: {"model": ErrorDto, "description": "EVIDENCIA_NO_CARGADA"},
        422: {"model": ErrorDto, "description": "INTEGRIDAD_EVIDENCIA_INVALIDA"},
        503: {"model": ErrorDto, "description": "DEPENDENCIA_NO_DISPONIBLE"},
    },
)
async def confirmar_evidencia(
    borrador_id: BorradorId,
    evidencia_id: EvidenciaId,
    actor: Cliente,
    casos: BorradoresDep,
) -> EvidenciaConfirmada:
    return EvidenciaConfirmada.model_validate(
        await casos.confirmar_evidencia(actor.id, borrador_id, evidencia_id)
    )


@router_borradores.post(
    "/borradores/{borradorId}/evidencias/{evidenciaId}/carga",
    response_model=CargaRenovada,
    response_model_by_alias=True,
    responses={
        404: {"model": ErrorDto, "description": "BORRADOR_NO_ENCONTRADO"},
        409: {"model": ErrorDto, "description": "EVIDENCIA_YA_CONFIRMADA"},
        503: {"model": ErrorDto, "description": "DEPENDENCIA_NO_DISPONIBLE"},
    },
)
async def renovar_carga(
    borrador_id: BorradorId,
    evidencia_id: EvidenciaId,
    actor: Cliente,
    casos: BorradoresDep,
) -> CargaRenovada:
    return CargaRenovada.model_validate(
        await casos.renovar_carga(actor.id, borrador_id, evidencia_id)
    )
