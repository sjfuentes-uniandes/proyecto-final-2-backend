"""Socios › alta, listado, detalle y desactivación (grupo administradores-socios, HU-W01).

El portal llama a <canales>/web/socios; API Gateway valida el token del pool
backoffice y sobrescribe X-Authenticated-Issuer y X-Authenticated-Groups.
"""

from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, Field

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.outbound.api_socios import ApiSocios, ErrorApiSocios
from bff_web.config import settings

router = APIRouter(
    prefix="/socios", tags=["socios"], dependencies=[Depends(requerir_grupo(settings.grupo_administradores_socios))]
)


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


class Estado(StrEnum):
    ACTIVO = "activo"
    DESACTIVADO = "desactivado"


class NuevoSocioSolicitud(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(min_length=2, max_length=120, description="Razón social")
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
        if error.status in (400, 404, 409):
            raise HTTPException(error.status, error.detalle or "Solicitud rechazada por api-socios") from error
        raise HTTPException(502, "api-socios no respondió correctamente") from error


@router.get("", response_model=list[SocioRespuesta], response_model_by_alias=True)
async def listar_socios(cliente: Cliente, administrador: Administrador = None) -> list[dict[str, Any]]:
    return await _proteger(cliente.listar(administrador))


@router.post("", response_model=SocioCreado, response_model_by_alias=True, status_code=201)
async def habilitar_socio(solicitud: NuevoSocioSolicitud, cliente: Cliente, http: Response,
                          administrador: Administrador = None) -> dict[str, Any]:
    creado = await _proteger(cliente.crear(solicitud.model_dump(mode="json"), administrador))
    http.headers["Cache-Control"] = "no-store"
    return creado


@router.get("/{socio_id}", response_model=SocioRespuesta, response_model_by_alias=True)
async def consultar_socio(socio_id: IdSocio, cliente: Cliente, administrador: Administrador = None) -> dict[str, Any]:
    return await _proteger(cliente.obtener(socio_id, administrador))


@router.post("/{socio_id}/desactivar", response_model=SocioRespuesta, response_model_by_alias=True)
async def desactivar_socio(socio_id: IdSocio, cliente: Cliente, administrador: Administrador = None) -> dict[str, Any]:
    return await _proteger(cliente.desactivar(socio_id, administrador))
