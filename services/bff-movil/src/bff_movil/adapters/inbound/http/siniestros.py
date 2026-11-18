"""API móvil para borradores de siniestro y captura de evidencia (HU-M01)."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator
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
    ActualizarBorrador,
    AgregarEvidencia,
    ConfirmarEvidencia,
    ConsultarBorrador,
    ConsultarLimitesEvidencia,
    ConsultarSiniestro,
    CrearBorrador,
    EliminarEvidencia,
    EnlazarEvidencia,
    EnviarBorrador,
    ListarBorradores,
    ListarSiniestros,
    RenovarCargaEvidencia,
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


class EnvioDto(Dto):
    borrador_id: UUID
    siniestro_id: UUID
    codigo: str = Field(pattern=r"^88-[0-9]{4}-[0-9]{4,}$")
    estado: Literal["REPORTADO"]
    reportado_en: AwareDatetime


class EnlaceEvidenciaDto(Dto):
    url: str = Field(pattern=r"^https://")
    expira_en: AwareDatetime
    nombre_archivo: str
    tipo_contenido: str


class SolicitudEnvio(Dto):
    """El envio no modifica datos: se envian los datos guardados mediante PATCH."""

    model_config = ConfigDict(extra="forbid")


class ActualizacionBorrador(Dto):
    model_config = ConfigDict(extra="forbid")

    descripcion: str | None = Field(default=None, min_length=1, max_length=2000)
    causa: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Codigo de cobertura/amparo de la poliza, separado de descripcion",
    )
    fecha_evento: AwareDatetime | None = Field(
        default=None, description="Fecha real del evento, con zona horaria; obligatoria al enviar"
    )

    @field_validator("descripcion", "causa")
    @classmethod
    def _texto_no_vacio(cls, valor: str | None) -> str | None:
        if valor is not None and not valor.strip():
            raise ValueError("el texto no puede estar vacio")
        return valor.strip() if valor is not None else None

    @model_validator(mode="after")
    def _cambios_validos(self) -> "ActualizacionBorrador":
        if not self.model_fields_set:
            raise ValueError("se requiere al menos un campo")
        if "descripcion" in self.model_fields_set and self.descripcion is None:
            raise ValueError("descripcion no puede ser null")
        return self


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
    estado_carga: Literal["PENDIENTE", "CARGADA"]
    carga_confirmada_en: AwareDatetime | None
    carga: CargaDto


class BorradorResumenDto(Dto):
    borrador_id: UUID
    poliza_id: UUID
    descripcion: str
    estado: Literal["ABIERTO", "CONVERTIDO"]
    creado_en: AwareDatetime
    cantidad_evidencias: int
    evidencias_pendientes: int
    causa: str | None = None
    fecha_evento: AwareDatetime | None = None
    version: int
    envio: EnvioDto | None = None


class EvidenciaBorradorDto(Dto):
    evidencia_id: UUID
    nombre_archivo: str
    tipo: Literal["FOTOGRAFIA", "VIDEO"]
    tipo_contenido: str
    tamano_bytes: int
    sha256: str
    capturada_en: AwareDatetime | None
    duracion_segundos: int | None
    estado_carga: Literal["PENDIENTE", "CARGADA"]
    carga_confirmada_en: AwareDatetime | None


class ListadoBorradoresDto(Dto):
    items: list[BorradorResumenDto]


class BorradorDetalleDto(BorradorResumenDto):
    evidencias: list[EvidenciaBorradorDto]


class EvidenciaConfirmadaDto(Dto):
    evidencia_id: UUID
    estado_carga: Literal["CARGADA"]
    carga_confirmada_en: AwareDatetime


class CargaRenovadaDto(Dto):
    evidencia_id: UUID
    estado_carga: Literal["PENDIENTE"]
    carga: CargaDto


EstadoSiniestro = Literal["REPORTADO", "EN_EVALUACION", "FALTA_SOPORTE", "APROBADO", "RECHAZADO"]


class SiniestroResumenDto(Dto):
    siniestro_id: UUID
    codigo: str
    origen: Literal["MOVIL", "PORTAL", "PARAMETRICO"]
    estado: EstadoSiniestro
    poliza_id: UUID
    reportado_en: AwareDatetime


class ListadoSiniestrosDto(Dto):
    items: list[SiniestroResumenDto]
    siguiente_cursor: str | None


class EvidenciaReporteDto(Dto):
    evidencia_id: UUID
    nombre_archivo: str
    tipo: Literal["FOTOGRAFIA", "VIDEO", "SOPORTE"]
    tipo_contenido: str | None
    tamano_bytes: int | None
    capturada_en: AwareDatetime | None


class HitoReporteDto(Dto):
    accion: str
    estado_anterior: EstadoSiniestro | None
    estado_nuevo: EstadoSiniestro | None
    ocurrido_en: AwareDatetime


class SiniestroDetalleDto(SiniestroResumenDto):
    descripcion: str | None
    causa: str
    fecha_evento: AwareDatetime
    evidencias: list[EvidenciaReporteDto]
    historial: list[HitoReporteDto]


_NO_AUTENTICADO = error("NO_AUTENTICADO: se requiere un cliente autenticado")
_ROL = error("ROL_NO_AUTORIZADO: el token no es del pool de clientes")
_NO_DISPONIBLE = error("SERVICIO_NO_DISPONIBLE: no se pudo consultar o crear el reporte")
_POLIZA_NO_VIGENTE = error("POLIZA_NO_VIGENTE: la póliza no está activa y vigente")
_BORRADOR_NO_ENCONTRADO = error("BORRADOR_NO_ENCONTRADO")
_EVIDENCIA_INVALIDA = error(
    "TIPO_EVIDENCIA_NO_ADMITIDO, TAMANO_EVIDENCIA_EXCEDIDO, DURACION_VIDEO_EXCEDIDA "
    "o LIMITE_EVIDENCIAS_ALCANZADO"
)
_EVIDENCIA_NO_CARGADA = error("EVIDENCIA_NO_CARGADA o BORRADOR_CONVERTIDO")
_EVIDENCIA_YA_CONFIRMADA = error("EVIDENCIA_YA_CONFIRMADA o BORRADOR_CONVERTIDO")
_INTEGRIDAD_INVALIDA = error("INTEGRIDAD_EVIDENCIA_INVALIDA")


def _error_cliente_no_encontrado() -> ErrorNegocio:
    return ErrorNegocio(CLIENTE_NO_ENCONTRADO, "El cliente no existe", 404)


def _error_poliza_no_encontrada() -> ErrorNegocio:
    return ErrorNegocio(POLIZA_NO_ENCONTRADA, "La póliza no existe", 404)


def _error_poliza_no_autorizada() -> ErrorNegocio:
    return ErrorNegocio(POLIZA_NO_AUTORIZADA, "La póliza no es del cliente", 403)


def _error_no_disponible() -> ErrorNegocio:
    return ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Servicio no disponible", 503)


@router.patch(
    "/borradores/{borradorId}",
    response_model=BorradorDetalleDto,
    responses={
        401: _NO_AUTENTICADO,
        403: _ROL,
        404: _BORRADOR_NO_ENCONTRADO,
        409: error("BORRADOR_CONVERTIDO"),
        422: error("SOLICITUD_INVALIDA"),
        503: _NO_DISPONIBLE,
    },
)
async def actualizar_borrador(
    borrador_id: Annotated[UUID, Path(alias="borradorId")],
    solicitud: ActualizacionBorrador,
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> BorradorDetalleDto:
    caso: ActualizarBorrador = request.app.state.actualizar_borrador
    try:
        return BorradorDetalleDto.model_validate(
            await caso.ejecutar(
                identidad.sub,
                borrador_id,
                solicitud.model_dump(by_alias=True, exclude_unset=True, mode="json"),
            )
        )
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error


@router.post(
    "/borradores/{borradorId}/envio",
    status_code=201,
    response_model=EnvioDto,
    responses={
        401: _NO_AUTENTICADO,
        403: error("ROL_NO_AUTORIZADO o POLIZA_NO_AUTORIZADA"),
        404: error("BORRADOR_NO_ENCONTRADO, CLIENTE_NO_ENCONTRADO o POLIZA_NO_ENCONTRADA"),
        409: error("POLIZA_NO_VIGENTE, EVIDENCIA_NO_CARGADA o BORRADOR_MODIFICADO"),
        422: error("SOLICITUD_INVALIDA, IDEMPOTENCIA_CONFLICTO o INTEGRIDAD_EVIDENCIA_INVALIDA"),
        503: _NO_DISPONIBLE,
    },
)
async def enviar_borrador(
    borrador_id: Annotated[UUID, Path(alias="borradorId")],
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
    clave: Annotated[UUID, Header(alias="Idempotency-Key")],
    solicitud: SolicitudEnvio | None = None,
) -> EnvioDto:
    caso: EnviarBorrador = request.app.state.enviar_borrador
    try:
        return EnvioDto.model_validate(await caso.ejecutar(identidad.sub, borrador_id, clave))
    except ClienteNoEncontrado as error:
        raise _error_cliente_no_encontrado() from error
    except PolizaNoEncontrada as error:
        raise _error_poliza_no_encontrada() from error
    except PolizaNoAutorizada as error:
        raise _error_poliza_no_autorizada() from error
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error


@router.delete(
    "/borradores/{borradorId}/evidencias/{evidenciaId}",
    status_code=204,
    responses={
        401: _NO_AUTENTICADO,
        403: _ROL,
        404: _BORRADOR_NO_ENCONTRADO,
        409: error("BORRADOR_CONVERTIDO"),
        503: _NO_DISPONIBLE,
    },
)
async def eliminar_evidencia(
    borrador_id: Annotated[UUID, Path(alias="borradorId")],
    evidencia_id: Annotated[UUID, Path(alias="evidenciaId")],
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> Response:
    caso: EliminarEvidencia = request.app.state.eliminar_evidencia
    try:
        await caso.ejecutar(identidad.sub, borrador_id, evidencia_id)
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error
    return Response(status_code=204)


@router.get(
    "",
    response_model=ListadoSiniestrosDto,
    responses={
        401: _NO_AUTENTICADO,
        403: _ROL,
        404: error("CLIENTE_NO_ENCONTRADO"),
        422: error("SOLICITUD_INVALIDA"),
        503: _NO_DISPONIBLE,
    },
)
async def listar_siniestros(
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
    limite: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> ListadoSiniestrosDto:
    caso: ListarSiniestros = request.app.state.listar_siniestros
    try:
        return ListadoSiniestrosDto.model_validate(
            await caso.ejecutar(identidad.sub, limite, cursor)
        )
    except ClienteNoEncontrado as error:
        raise _error_cliente_no_encontrado() from error
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error


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


@router.get(
    "/borradores",
    response_model=ListadoBorradoresDto,
    response_model_by_alias=True,
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: _ROL,
        422: error("SOLICITUD_INVALIDA"),
        503: _NO_DISPONIBLE,
    },
)
async def listar_borradores(
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> ListadoBorradoresDto:
    caso: ListarBorradores = request.app.state.listar_borradores
    try:
        return ListadoBorradoresDto.model_validate(await caso.ejecutar(identidad.sub))
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error


@router.get(
    "/borradores/{borradorId}",
    response_model=BorradorDetalleDto,
    response_model_by_alias=True,
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: _ROL,
        404: _BORRADOR_NO_ENCONTRADO,
        422: error("SOLICITUD_INVALIDA"),
        503: _NO_DISPONIBLE,
    },
)
async def consultar_borrador(
    borrador_id: Annotated[UUID, Path(alias="borradorId")],
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> BorradorDetalleDto:
    caso: ConsultarBorrador = request.app.state.consultar_borrador
    try:
        return BorradorDetalleDto.model_validate(await caso.ejecutar(identidad.sub, borrador_id))
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
        409: error("BORRADOR_CONVERTIDO"),
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


@router.post(
    "/borradores/{borradorId}/evidencias/{evidenciaId}/confirmacion",
    response_model=EvidenciaConfirmadaDto,
    response_model_by_alias=True,
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: _ROL,
        404: _BORRADOR_NO_ENCONTRADO,
        409: _EVIDENCIA_NO_CARGADA,
        422: _INTEGRIDAD_INVALIDA,
        503: _NO_DISPONIBLE,
    },
)
async def confirmar_evidencia(
    borrador_id: Annotated[UUID, Path(alias="borradorId")],
    evidencia_id: Annotated[UUID, Path(alias="evidenciaId")],
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> EvidenciaConfirmadaDto:
    caso: ConfirmarEvidencia = request.app.state.confirmar_evidencia
    try:
        return EvidenciaConfirmadaDto.model_validate(
            await caso.ejecutar(identidad.sub, borrador_id, evidencia_id)
        )
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error


@router.post(
    "/borradores/{borradorId}/evidencias/{evidenciaId}/carga",
    response_model=CargaRenovadaDto,
    response_model_by_alias=True,
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: _ROL,
        404: _BORRADOR_NO_ENCONTRADO,
        409: _EVIDENCIA_YA_CONFIRMADA,
        503: _NO_DISPONIBLE,
    },
)
async def renovar_carga(
    borrador_id: Annotated[UUID, Path(alias="borradorId")],
    evidencia_id: Annotated[UUID, Path(alias="evidenciaId")],
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> CargaRenovadaDto:
    caso: RenovarCargaEvidencia = request.app.state.renovar_carga_evidencia
    try:
        return CargaRenovadaDto.model_validate(
            await caso.ejecutar(identidad.sub, borrador_id, evidencia_id)
        )
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error


@router.post(
    "/{siniestroId}/evidencias/{evidenciaId}/enlace",
    response_model=EnlaceEvidenciaDto,
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: _ROL,
        404: error(
            "CLIENTE_NO_ENCONTRADO, SINIESTRO_NO_ENCONTRADO, EVIDENCIA_NO_ENCONTRADA "
            "o EVIDENCIA_NO_DISPONIBLE"
        ),
        409: error("EVIDENCIA_NO_CARGADA"),
        422: error("SOLICITUD_INVALIDA o INTEGRIDAD_EVIDENCIA_INVALIDA"),
        503: _NO_DISPONIBLE,
    },
)
async def enlace_evidencia(
    siniestro_id: Annotated[UUID, Path(alias="siniestroId")],
    evidencia_id: Annotated[UUID, Path(alias="evidenciaId")],
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> EnlaceEvidenciaDto:
    caso: EnlazarEvidencia = request.app.state.enlazar_evidencia
    try:
        resultado = await caso.ejecutar(identidad.sub, siniestro_id, evidencia_id)
        return EnlaceEvidenciaDto.model_validate(resultado)
    except ClienteNoEncontrado as error:
        raise _error_cliente_no_encontrado() from error
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error


@router.get(
    "/{siniestroId}",
    response_model=SiniestroDetalleDto,
    responses={
        401: _NO_AUTENTICADO,
        403: _ROL,
        404: error("SINIESTRO_NO_ENCONTRADO o CLIENTE_NO_ENCONTRADO"),
        503: _NO_DISPONIBLE,
    },
)
async def consultar_siniestro(
    siniestro_id: Annotated[UUID, Path(alias="siniestroId")],
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> SiniestroDetalleDto:
    caso: ConsultarSiniestro = request.app.state.consultar_siniestro
    try:
        return SiniestroDetalleDto.model_validate(await caso.ejecutar(identidad.sub, siniestro_id))
    except ClienteNoEncontrado as error:
        raise _error_cliente_no_encontrado() from error
    except ServicioNoDisponible as error:
        raise _error_no_disponible() from error
