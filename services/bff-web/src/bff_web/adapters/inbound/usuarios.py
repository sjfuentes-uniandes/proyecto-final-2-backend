"""Administración › Usuarios: alta y listado de usuarios del back-office (grupo administradores)."""

import asyncio
from datetime import datetime
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.outbound.cognito import crear_directorio
from bff_web.application.usuarios import AdministrarUsuarios
from bff_web.config import settings
from bff_web.domain.usuarios import GRUPOS, NuevoUsuario, Usuario, UsuarioYaExiste

router = APIRouter(
    prefix="/backoffice", tags=["administracion"], dependencies=[Depends(requerir_grupo(settings.grupo_administradores))]
)


class UsuarioRespuesta(BaseModel):
    email: str
    nombre: str | None
    estado: str
    habilitado: bool
    creado: datetime | None
    grupos: list[str]


class GrupoRespuesta(BaseModel):
    nombre: str
    descripcion: str


class NuevoUsuarioSolicitud(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    nombre: str = Field(min_length=2, max_length=120)
    grupos: list[str] = Field(min_length=1, max_length=len(GRUPOS))


@lru_cache
def administrar_usuarios() -> AdministrarUsuarios:
    if not settings.backoffice_user_pool_id:
        raise HTTPException(503, "Falta BACKOFFICE_USER_POOL_ID")
    return AdministrarUsuarios(crear_directorio(settings.aws_region, settings.backoffice_user_pool_id))


Caso = Annotated[AdministrarUsuarios, Depends(administrar_usuarios)]


def _respuesta(usuario: Usuario) -> UsuarioRespuesta:
    return UsuarioRespuesta(**usuario.__dict__)


@router.get("/grupos", response_model=list[GrupoRespuesta])
async def listar_grupos() -> list[GrupoRespuesta]:
    return [GrupoRespuesta(nombre=nombre, descripcion=descripcion) for nombre, descripcion in GRUPOS.items()]


@router.get("/usuarios", response_model=list[UsuarioRespuesta])
async def listar_usuarios(caso: Caso) -> list[UsuarioRespuesta]:
    return [_respuesta(u) for u in await asyncio.to_thread(caso.listar)]


@router.post("/usuarios", response_model=UsuarioRespuesta, status_code=201)
async def crear_usuario(solicitud: NuevoUsuarioSolicitud, caso: Caso) -> UsuarioRespuesta:
    try:
        nuevo = NuevoUsuario(email=solicitud.email.strip().lower(), nombre=solicitud.nombre, grupos=tuple(dict.fromkeys(solicitud.grupos)))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    try:
        return _respuesta(await asyncio.to_thread(caso.crear, nuevo))
    except UsuarioYaExiste as error:
        raise HTTPException(409, "Ya existe un usuario con ese correo") from error
