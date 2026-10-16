"""Registro de un cliente nuevo: verificación antes de la cuenta y vínculo al primer ingreso."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from bff_movil.domain.sesion.modelos import EstadoIdentidad
from bff_movil.domain.verificacion.modelos import ResultadoKyc


@dataclass(frozen=True, slots=True)
class RegistroVerificado:
    cliente_id: UUID
    estado: EstadoIdentidad
    resultado: ResultadoKyc
    motivo: str | None
    verificado_en: datetime | None
    token_registro: str | None
    expira_en: datetime | None


@dataclass(frozen=True, slots=True)
class ClienteVinculado:
    cliente_id: UUID
    estado: EstadoIdentidad
    verificado_en: datetime | None


class RegistroConCorreoDistinto(Exception):
    """El registro ya se verificó con otro correo."""


class RegistroYaVinculado(Exception):
    """La cuenta de este registro ya existe, o el titular ya pertenece a otro cliente."""


class TokenRegistroInvalido(Exception):
    """El token de registro no sirve. `motivo` nunca incluye el token ni sus datos."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo
