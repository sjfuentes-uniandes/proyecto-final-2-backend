"""Rutas que consume el canal del socio a través del API Gateway "socios".

El borde ya validó el token client_credentials y la API key (401 si no) y
sobrescribe X-Partner-Client-Id, X-Partner-Scopes y X-Partner-Key-Id. Aquí se
confirma que el socio exista y siga activo con su propia API key (AC2), se exige
el permiso de cada operación (AC4) y solo se devuelven sus propios datos.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from solventa_common.errors import ErrorNegocio

from api_socios.adapters.inbound.dependencias import autenticar_socio
from api_socios.adapters.inbound.errores import (
    OPERACION_NO_DISPONIBLE,
    PERMISO_NO_ASIGNADO,
    error,
    no_autenticado,
)
from api_socios.application.socios import AutenticarSocio
from api_socios.config import settings
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
    # Los permisos salen del token emitido por Cognito, acotados a los que tiene el socio.
    permisos = permisos_de_alcances(x_partner_scopes, settings.partners_resource_server) & socio.permisos
    return SocioAutenticado(socio=socio, permisos=permisos)


Autenticado = Annotated[SocioAutenticado, Depends(socio_autenticado)]


async def autorizado(request: Request, autenticado: Autenticado) -> SocioAutenticado:
    """Busca el permiso de la ruta en PERMISO_POR_OPERACION; sin él responde 403."""
    permiso = permiso_requerido(request.method, request.scope["route"].path)
    if permiso not in autenticado.permisos:
        raise ErrorNegocio(PERMISO_NO_ASIGNADO, "El socio no tiene permiso para esta operación", 403,
                           {"permiso": permiso.value})
    return autenticado


Autorizado = Annotated[SocioAutenticado, Depends(autorizado)]

router = APIRouter(tags=["socio"], responses={
    401: error("NO_AUTENTICADO: credenciales inexistentes, incorrectas o desactivadas"),
    403: error("PERMISO_NO_ASIGNADO (detalles.permiso)"),
    422: error("SOLICITUD_INVALIDA (detalles.campos)"),
})


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
    return PerfilSocio(id=socio.id, nombre=socio.nombre, canal=socio.canal, plan=socio.plan,
                       permisos=sorted(autenticado.permisos), estado=socio.estado, client_id=socio.client_id,
                       creado=socio.creado)


def _no_disponible() -> ErrorNegocio:
    return ErrorNegocio(OPERACION_NO_DISPONIBLE, "Operación aún no disponible para socios", 501)


# Operaciones de negocio: la autorización ya aplica; cotizacion y polizas las implementan
# en historias posteriores (al reenviarlas se debe filtrar por autenticado.socio.id).
@router.post("/cotizaciones", status_code=201, responses={501: error("OPERACION_NO_DISPONIBLE")})
async def solicitar_cotizacion(autenticado: Autorizado) -> None:
    raise _no_disponible()


@router.get("/cotizaciones/{cotizacion_id}", responses={501: error("OPERACION_NO_DISPONIBLE")})
async def consultar_cotizacion(autenticado: Autorizado, cotizacion_id: Annotated[str, Path(max_length=64)]) -> None:
    raise _no_disponible()


@router.post("/polizas", status_code=201, responses={501: error("OPERACION_NO_DISPONIBLE")})
async def emitir_poliza(autenticado: Autorizado) -> None:
    raise _no_disponible()


@router.get("/polizas/{poliza_id}", responses={501: error("OPERACION_NO_DISPONIBLE")})
async def consultar_poliza(autenticado: Autorizado, poliza_id: Annotated[str, Path(max_length=64)]) -> None:
    raise _no_disponible()


@router.post("/siniestros", status_code=201, responses={501: error("OPERACION_NO_DISPONIBLE")})
async def avisar_siniestro(autenticado: Autorizado) -> None:
    raise _no_disponible()
