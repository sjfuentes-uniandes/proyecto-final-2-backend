"""Usuarios del back-office (pool de Cognito "backoffice")."""

import re
from dataclasses import dataclass, field
from datetime import datetime

# Grupos que se pueden asignar desde el portal (infra/platform/identity.tf).
GRUPOS = {
    "administradores": "Crea usuarios del back-office y asigna sus grupos",
    "administradores-socios": "Gestiona socios, credenciales y cuotas",
    "operacion": "Consulta tableros, trazas y alertas",
}
_CORREO = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class UsuarioYaExiste(Exception):
    pass


@dataclass(frozen=True)
class NuevoUsuario:
    email: str
    nombre: str
    grupos: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _CORREO.fullmatch(self.email) or len(self.email) > 254:
            raise ValueError("Correo inválido")
        if not 2 <= len(self.nombre.strip()) <= 120:
            raise ValueError("Nombre inválido")
        if not self.grupos or not set(self.grupos) <= set(GRUPOS):
            raise ValueError("Grupos inválidos")


@dataclass
class Usuario:
    email: str
    nombre: str | None
    estado: str  # FORCE_CHANGE_PASSWORD, CONFIRMED, ...
    habilitado: bool
    creado: datetime | None
    grupos: list[str] = field(default_factory=list)
