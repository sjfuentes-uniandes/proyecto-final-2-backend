"""API móvil para borradores de siniestro y captura de evidencia (HU-M01)."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request
from pydantic import AwareDatetime, Field, field_validator, model_validator
from solventa_common.errors import ErrorNegocio

from bff_movil.adapters.inbound.http.auth import IdentidadAutenticada, autenticar
from bff_movil.adapters.inbound.http.codigos import (
    CLIENTE_NO_ENCONTRADO,
    POLIZA_NO_AUTORIZADA,
    POLIZA_NO_ENCONTRADA,
    SERVICIO_NO_DISPONIBLE,
)
from bff_movil.adapters.inbound.http.comun import CABECERAS, Dto, error
from bff_movil.application.siniestros.casos import (
    AgregarEvidencia,
    ConsultarLimitesEvidencia,
    CrearBorrador,
)
from bff_movil.domain.polizas.modelos import PolizaNoAutorizada, PolizaNoEncontrada
from bff_movil.domain.sesion.errores import (
    ClienteNoEncontrado,
    ServicioNoDisponible,
)
from bff_movil.domain.siniestros.errores import PolizaNoVigente

router = APIRouter(prefix="/v1/siniestros", tags=["siniestros"])
DESCRIPCION_MAX_CARACTERES = 2000


class LimitesDto(Dto):
    max_evidencias: int
    formatos_foto: list[str]
    formatos_video: list[str]
    tamano_maximo_foto_bytes: int
    tamano_maximo_video_bytes: int
    duracion_maxima_video_segundos: int
    url_carga_expira_segundos: int


class SolicitudBorrador(Dto):
    poliza_id: UUID
    descripcion: str = Field(min_length=1, max_length=DESCRIPCION_MAX_CARACTERES)

    @field_validator("descripcion")
    @classmethod
    def _descripcion_no_vacia(cls, valor: str) -> str:
        descripcion = valor.strip()
        if not descripcion:
            raise ValueError("descripcion es obligatoria")
        return descripcion


class BorradorDto(Dto):
    borrador_id: UUID
    poliza_id: UUID
    estado: str


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
        if (self.tipo == "VIDEO") != (self.duracion_segundos is not None):
            raise ValueError("duracionSegundos es obligatorio solo para videos")
        return self


class CargaDto(Dto):
    metodo: str
    url: str
    campos: dict[str, str]
    expira_en: AwareDatetime


class EvidenciaDto(Dto):
    evidencia_id: UUID
    nombre_archivo: str
    tipo: Literal["FOTOGRAFIA", "VIDEO"]
    tipo_contenido: str
    tamano_bytes: int
    sha256: str
    capturada_en: AwareDatetime | None
    duracion_segundos: int | None
    carga: CargaDto


_NO_AUTENTICADO = error("NO_AUTENTICADO: se requiere un cliente autenticado")
_ROL = error("ROL_NO_AUTORIZADO: el token no es del pool de clientes")
_NO_DISPONIBLE = error("SERVICIO_NO_DISPONIBLE: no se pudo consultar o crear el reporte")
_POLIZA_NO_VIGENTE = error("POLIZA_NO_VIGENTE: la póliza no está emitida y vigente")
_BORRADOR_NO_ENCONTRADO = error("BORRADOR_NO_ENCONTRADO")
_EVIDENCIA_INVALIDA = error(
    "TIPO_EVIDENCIA_NO_ADMITIDO, TAMANO_EVIDENCIA_EXCEDIDO, DURACION_VIDEO_EXCEDIDA "
    "o LIMITE_EVIDENCIAS_ALCANZADO"
)


def _error_cliente_no_encontrado() -> ErrorNegocio:
    return ErrorNegocio(CLIENTE_NO_ENCONTRADO, "El cliente no existe", 404)


def _error_poliza_no_encontrada() -> ErrorNegocio:
    return ErrorNegocio(POLIZA_NO_ENCONTRADA, "La póliza no existe", 404)


def _error_poliza_no_autorizada() -> ErrorNegocio:
    return ErrorNegocio(POLIZA_NO_AUTORIZADA, "La póliza no es del cliente", 403)


def _error_no_disponible() -> ErrorNegocio:
    return ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Servicio no disponible", 503)


@router.get(
    "/limites",
    response_model=LimitesDto,
    response_model_by_alias=True,
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: _ROL,
        422: error("SOLICITUD_INVALIDA"),
        503: _NO_DISPONIBLE,
    },
)
async def limites(
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> LimitesDto:
    caso: ConsultarLimitesEvidencia = request.app.state.consultar_limites_evidencia
    try:
        return LimitesDto.model_validate(await caso.ejecutar(identidad.sub))
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error


@router.post(
    "/borradores",
    status_code=201,
    response_model=BorradorDto,
    response_model_by_alias=True,
    responses={
        201: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: error("ROL_NO_AUTORIZADO o POLIZA_NO_AUTORIZADA"),
        404: error("CLIENTE_NO_ENCONTRADO o POLIZA_NO_ENCONTRADA"),
        409: _POLIZA_NO_VIGENTE,
        400: error("SOLICITUD_INVALIDA"),
        422: error("SOLICITUD_INVALIDA o IDEMPOTENCIA_CONFLICTO"),
        503: _NO_DISPONIBLE,
    },
)
async def crear_borrador(
    solicitud: SolicitudBorrador,
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
    clave: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> BorradorDto:
    caso: CrearBorrador = request.app.state.crear_borrador
    try:
        return BorradorDto.model_validate(
            await caso.ejecutar(identidad.sub, solicitud.poliza_id, solicitud.descripcion, clave)
        )
    except PolizaNoVigente:
        raise
    except ClienteNoEncontrado as error:
        raise _error_cliente_no_encontrado() from error
    except PolizaNoEncontrada as error:
        raise _error_poliza_no_encontrada() from error
    except PolizaNoAutorizada as error:
        raise _error_poliza_no_autorizada() from error
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error


@router.post(
    "/borradores/{borradorId}/evidencias",
    status_code=201,
    response_model=EvidenciaDto,
    response_model_by_alias=True,
    responses={
        201: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: _ROL,
        404: _BORRADOR_NO_ENCONTRADO,
        400: error("SOLICITUD_INVALIDA"),
        422: _EVIDENCIA_INVALIDA,
        503: _NO_DISPONIBLE,
    },
)
async def agregar_evidencia(
    solicitud: SolicitudEvidencia,
    borrador_id: Annotated[UUID, Path(alias="borradorId")],
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> EvidenciaDto:
    caso: AgregarEvidencia = request.app.state.agregar_evidencia
    try:
        return EvidenciaDto.model_validate(
            await caso.ejecutar(
                identidad.sub, borrador_id, solicitud.model_dump(by_alias=True, mode="json")
            )
        )
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error
