"""Casos de uso: listar y crear usuarios del back-office."""

from typing import Protocol

from solventa_common.telemetry import operacion

from bff_web.domain.usuarios import NuevoUsuario, Usuario


class DirectorioUsuarios(Protocol):
    def listar(self) -> list[Usuario]: ...

    def crear(self, usuario: NuevoUsuario) -> Usuario: ...


class AdministrarUsuarios:
    def __init__(self, directorio: DirectorioUsuarios) -> None:
        self.directorio = directorio

    @operacion("listar_usuarios_backoffice")
    def listar(self) -> list[Usuario]:
        return sorted(self.directorio.listar(), key=lambda u: u.email)

    @operacion("crear_usuario_backoffice")
    def crear(self, usuario: NuevoUsuario) -> Usuario:
        # Cognito envía la contraseña temporal al correo (invite_message_template).
        return self.directorio.crear(usuario)
