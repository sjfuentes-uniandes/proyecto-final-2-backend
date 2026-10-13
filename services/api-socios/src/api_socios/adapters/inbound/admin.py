"""/admin/socios: alta, consulta y desactivación de socios. Solo la invoca bff-web.

API Gateway enruta todo el API de socios (/{proxy+}) hacia este servicio y
siempre sobrescribe X-Partner-Client-Id y X-Partner-Key-Id. Una solicitud de
administración con esos encabezados viene de un socio y se rechaza con 404
para no revelar la ruta. bff-web llega por Service Connect, sin esos encabezados.
"""

import asyncio
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Response
from pydantic import BaseModel, ConfigDict, Field
from solventa_common.errors import ErrorNegocio

from api_socios.adapters.inbound.dependencias import administrar_socios
from api_socios.adapters.inbound.errores import (
    SOLICITUD_INVALIDA,
    error,
    no_encontrado,
    socio_no_encontrado,
)
from api_socios.application.socios import AdministrarSocios
from api_socios.domain.socios import (
    CanalVenta,
    Estado,
    NuevoSocio,
    Permiso,
    Plan,
    Socio,
    SocioNoEncontrado,
)

logger = logging.getLogger("api_socios.admin")


def solo_administracion(
    x_partner_client_id: Annotated[str | None, Header(include_in_schema=False)] = None,
    x_partner_key_id: Annotated[str | None, Header(include_in_schema=False)] = None,
    x_authenticated_sub: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> str | None:
    if x_partner_client_id is not None or x_partner_key_id is not None:
        logger.warning("socio intentó usar la administración", extra={"client_id": x_partner_client_id})
        raise no_encontrado()
    return x_authenticated_sub


router = APIRouter(prefix="/admin/socios", tags=["administracion"], dependencies=[Depends(solo_administracion)],
                   responses={
                       400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
                       404: error("SOCIO_NO_ENCONTRADO, o NO_ENCONTRADO si la solicitud llega desde el API de socios"),
                       422: error("SOLICITUD_INVALIDA (detalles.campos)"),
                   })

Administrador = Annotated[str | None, Depends(solo_administracion)]
Caso = Annotated[AdministrarSocios, Depends(administrar_socios)]
IdSocio = Annotated[str, Path(pattern=r"^SOC-\d{6}$")]


class SocioSolicitud(BaseModel):
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
    desactivado: datetime | None


class SocioCreado(SocioRespuesta):
    """Única respuesta que contiene el secreto y la API key (AC1)."""

    client_secret: str = Field(alias="clientSecret")
    api_key: str = Field(alias="apiKey")


def respuesta(socio: Socio) -> SocioRespuesta:
    return SocioRespuesta(
        id=socio.id, nombre=socio.nombre, canal=socio.canal, plan=socio.plan, permisos=sorted(socio.permisos),
        estado=socio.estado, client_id=socio.client_id, creado=socio.creado, desactivado=socio.desactivado,
    )


def _buscar(caso: AdministrarSocios, socio_id: str) -> Socio:
    try:
        return caso.obtener(socio_id)
    except SocioNoEncontrado as error:
        raise socio_no_encontrado() from error


@router.get("", response_model=list[SocioRespuesta], response_model_by_alias=True)
async def listar_socios(caso: Caso) -> list[SocioRespuesta]:
    return [respuesta(s) for s in await asyncio.to_thread(caso.listar)]


@router.post("", response_model=SocioCreado, response_model_by_alias=True, status_code=201)
async def habilitar_socio(solicitud: SocioSolicitud, caso: Caso, administrador: Administrador,
                          http: Response) -> SocioCreado:
    try:
        nuevo = NuevoSocio(nombre=solicitud.nombre, canal=solicitud.canal, plan=solicitud.plan,
                           permisos=frozenset(solicitud.permisos))
    except ValueError as error:
        campo = "permisos" if "permiso" in str(error) else "nombre"
        raise ErrorNegocio(SOLICITUD_INVALIDA, str(error), 422, {"campos": [campo]}) from error
    socio, credenciales = await asyncio.to_thread(caso.habilitar, nuevo)
    logger.info("alta de socio solicitada", extra={"socio_id": socio.id, "administrador": administrador})
    # El secreto no debe quedar en cachés intermedias ni del navegador.
    http.headers["Cache-Control"] = "no-store"
    return SocioCreado(**respuesta(socio).model_dump(), client_secret=credenciales.client_secret,
                       api_key=credenciales.api_key)


@router.get("/{socio_id}", response_model=SocioRespuesta, response_model_by_alias=True)
async def consultar_socio(socio_id: IdSocio, caso: Caso) -> SocioRespuesta:
    return respuesta(await asyncio.to_thread(_buscar, caso, socio_id))


@router.post("/{socio_id}/desactivar", response_model=SocioRespuesta, response_model_by_alias=True)
async def desactivar_socio(socio_id: IdSocio, caso: Caso, administrador: Administrador) -> SocioRespuesta:
    try:
        socio = await asyncio.to_thread(caso.desactivar, socio_id)
    except SocioNoEncontrado as error:
        raise socio_no_encontrado() from error
    logger.info("desactivación de socio solicitada", extra={"socio_id": socio_id, "administrador": administrador})
    return respuesta(socio)
