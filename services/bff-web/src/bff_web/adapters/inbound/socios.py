from datetime import date, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, Path, Response
from pydantic import BaseModel, ConfigDict, Field
from solventa_common.errors import ErrorNegocio, RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.outbound.api_socios import ApiSocios, ErrorApiSocios
from bff_web.config import settings


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/socios",
    tags=["socios"],
    route_class=RutaB2,
    dependencies=[Depends(requerir_grupo(settings.grupo_administradores_socios))],
    responses={
        403: _error(
            "ROL_NO_AUTORIZADO: falta el grupo administradores-socios o no es del back-office"
        ),
        404: _error("SOCIO_NO_ENCONTRADO"),
        422: _error("SOLICITUD_INVALIDA (detalles.campos)"),
        503: _error("SERVICIO_NO_DISPONIBLE: api-socios no respondió"),
    },
)

_REENVIADOS = {404, 409, 422}


class Permiso(StrEnum):
    COTIZAR = "cotizar"
    EMITIR = "emitir"
    CONSULTAR = "consultar"
    SINIESTROS = "siniestros"


class CanalVenta(StrEnum):
    PLATAFORMA_DIGITAL = "plataforma_digital"
    ENTIDAD_FINANCIERA = "entidad_financiera"
    COMERCIO = "comercio"
    OTRO = "otro"


class Plan(StrEnum):
    BASICO = "basico"
    ESTANDAR = "estandar"
    PRUEBA = "prueba"


class Periodo(StrEnum):
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


class EstadoCuota(StrEnum):
    NORMAL = "normal"
    CERCA_DEL_LIMITE = "cerca_del_limite"
    AGOTADA = "agotada"


class Estado(StrEnum):
    ACTIVO = "activo"
    DESACTIVADO = "desactivado"


_CONTROL = r"\x00-\x1f\x7f-\x9f"
RAZON_SOCIAL = rf"^[^\S{_CONTROL}]*[^\s{_CONTROL}][^{_CONTROL}]*[^\s{_CONTROL}][^\S{_CONTROL}]*$"


class NuevoSocioSolicitud(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(
        min_length=2, max_length=120, pattern=RAZON_SOCIAL, description="Razón social"
    )
    canal: CanalVenta
    plan: Plan
    permisos: list[Permiso] = Field(min_length=1, max_length=len(Permiso))


class SocioRespuesta(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    nombre: str
    canal: CanalVenta
    plan: Plan
    permisos: list[Permiso]
    estado: Estado
    client_id: str = Field(alias="clientId")
    creado: datetime
    desactivado: datetime | None = None


class SocioCreado(SocioRespuesta):
    """Credenciales completas: el portal las muestra una sola vez y no se pueden volver a pedir."""

    client_secret: str = Field(alias="clientSecret")
    api_key: str = Field(alias="apiKey")


class CambioPlanSolicitud(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: Plan


class LimitesPlan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    plan: Plan
    limite_por_segundo: float = Field(alias="limitePorSegundo")
    rafaga: int
    cuota: int
    periodo: Periodo


class VentanaCuota(BaseModel):
    inicio: date
    fin: date
    reinicio: datetime


class ConsumoSocio(BaseModel):
    """Solo datos del socio consultado: el administrador nunca ve la API key."""

    model_config = ConfigDict(populate_by_name=True)

    socio_id: str = Field(alias="socioId")
    limites: LimitesPlan
    ventana: VentanaCuota
    usado: int
    restante: int
    porcentaje: int
    estado: EstadoCuota
    limitadas: int | None = Field(
        None, description="Respuestas 429 en la ventana; null si no hay métricas"
    )


@lru_cache
def api_socios() -> ApiSocios:
    return ApiSocios(settings.api_socios_url)


Cliente = Annotated[ApiSocios, Depends(api_socios)]
Administrador = Annotated[str | None, Header(alias="X-Authenticated-Sub", include_in_schema=False)]
IdSocio = Annotated[str, Path(pattern=r"^SOC-\d{6}$")]


async def _proteger(llamada: Any) -> Any:
    try:
        return await llamada
    except ErrorApiSocios as error:
        if error.status in _REENVIADOS and error.codigo:
            raise ErrorNegocio(
                error.codigo, error.mensaje or "Solicitud rechazada", error.status, error.detalles
            ) from error
        raise ErrorNegocio(
            "SERVICIO_NO_DISPONIBLE", "api-socios no respondió correctamente", 503
        ) from error
    except httpx.HTTPError as error:
        raise ErrorNegocio("SERVICIO_NO_DISPONIBLE", "api-socios no respondió", 503) from error


@router.get("", response_model=list[SocioRespuesta], response_model_by_alias=True)
async def listar_socios(
    cliente: Cliente, administrador: Administrador = None
) -> list[dict[str, Any]]:
    return await _proteger(cliente.listar(administrador))


@router.post("", response_model=SocioCreado, response_model_by_alias=True, status_code=201)
async def habilitar_socio(
    solicitud: NuevoSocioSolicitud,
    cliente: Cliente,
    http: Response,
    administrador: Administrador = None,
) -> dict[str, Any]:
    creado = await _proteger(cliente.crear(solicitud.model_dump(mode="json"), administrador))
    http.headers["Cache-Control"] = "no-store"
    return creado


@router.get("/planes", response_model=list[LimitesPlan], response_model_by_alias=True)
async def listar_planes(
    cliente: Cliente, administrador: Administrador = None
) -> list[dict[str, Any]]:
    return await _proteger(cliente.planes(administrador))


@router.get("/{socio_id}", response_model=SocioRespuesta, response_model_by_alias=True)
async def consultar_socio(
    socio_id: IdSocio, cliente: Cliente, administrador: Administrador = None
) -> dict[str, Any]:
    return await _proteger(cliente.obtener(socio_id, administrador))


@router.post("/{socio_id}/desactivar", response_model=SocioRespuesta, response_model_by_alias=True)
async def desactivar_socio(
    socio_id: IdSocio, cliente: Cliente, administrador: Administrador = None
) -> dict[str, Any]:
    return await _proteger(cliente.desactivar(socio_id, administrador))


@router.put(
    "/{socio_id}/plan",
    response_model=SocioRespuesta,
    response_model_by_alias=True,
    responses={
        409: _error("SOCIO_DESACTIVADO"),
        422: _error("SOLICITUD_INVALIDA o PLAN_NO_DISPONIBLE (detalles.plan)"),
    },
)
async def cambiar_plan(
    socio_id: IdSocio,
    solicitud: CambioPlanSolicitud,
    cliente: Cliente,
    administrador: Administrador = None,
) -> dict[str, Any]:
    return await _proteger(cliente.cambiar_plan(socio_id, solicitud.plan.value, administrador))


@router.get("/{socio_id}/consumo", response_model=ConsumoSocio, response_model_by_alias=True)
async def consultar_consumo(
    socio_id: IdSocio, cliente: Cliente, administrador: Administrador = None
) -> dict[str, Any]:
    return await _proteger(cliente.consumo(socio_id, administrador))
