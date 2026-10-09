"""Directorio de usuarios del back-office sobre Amazon Cognito."""

from collections.abc import Mapping
from typing import Any

from bff_web.domain.usuarios import GRUPOS, NuevoUsuario, Usuario, UsuarioYaExiste


def _atributos(usuario: Mapping[str, Any]) -> dict[str, str]:
    return {a["Name"]: a["Value"] for a in usuario.get("Attributes", usuario.get("UserAttributes", []))}


def _usuario(datos: Mapping[str, Any], grupos: list[str]) -> Usuario:
    atributos = _atributos(datos)
    return Usuario(
        email=atributos.get("email", str(datos.get("Username"))),
        nombre=atributos.get("name"),
        estado=str(datos.get("UserStatus", "")),
        habilitado=bool(datos.get("Enabled", True)),
        creado=datos.get("UserCreateDate"),
        grupos=sorted(grupos),
    )


class DirectorioCognito:
    def __init__(self, cliente: Any, pool_id: str) -> None:
        self.cliente = cliente
        self.pool_id = pool_id

    def _paginar(self, operacion: str, **parametros: Any) -> list[Mapping[str, Any]]:
        paginador = self.cliente.get_paginator(operacion)
        return [u for pagina in paginador.paginate(UserPoolId=self.pool_id, **parametros) for u in pagina["Users"]]

    def listar(self) -> list[Usuario]:
        grupos_por_usuario: dict[str, list[str]] = {}
        for grupo in GRUPOS:
            for usuario in self._paginar("list_users_in_group", GroupName=grupo):
                grupos_por_usuario.setdefault(str(usuario["Username"]), []).append(grupo)
        return [_usuario(u, grupos_por_usuario.get(str(u["Username"]), [])) for u in self._paginar("list_users")]

    def crear(self, nuevo: NuevoUsuario) -> Usuario:
        try:
            respuesta = self.cliente.admin_create_user(
                UserPoolId=self.pool_id,
                Username=nuevo.email,
                UserAttributes=[
                    {"Name": "email", "Value": nuevo.email},
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": "name", "Value": nuevo.nombre.strip()},
                ],
                DesiredDeliveryMediums=["EMAIL"],
            )
        except self.cliente.exceptions.UsernameExistsException as error:
            raise UsuarioYaExiste(nuevo.email) from error
        for grupo in nuevo.grupos:
            self.cliente.admin_add_user_to_group(UserPoolId=self.pool_id, Username=nuevo.email, GroupName=grupo)
        return _usuario(respuesta["User"], list(nuevo.grupos))


def crear_directorio(region: str, pool_id: str) -> DirectorioCognito:
    import boto3

    return DirectorioCognito(boto3.client("cognito-idp", region_name=region), pool_id)
