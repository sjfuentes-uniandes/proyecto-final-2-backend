"""Identidad del cliente. Puro: sin E/S."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class EstadoIdentidad(StrEnum):
    VERIFICADO = "VERIFICADO"
    PENDIENTE = "PENDIENTE"
    RECHAZADO = "RECHAZADO"


class ResultadoKyc(StrEnum):
    """Veredicto del proveedor de identidad, tal como lo entrega `adaptador-identidad`."""

    APROBADO = "APROBADO"
    CAPTURA_INVALIDA = "CAPTURA_INVALIDA"
    RECHAZADO = "RECHAZADO"


@dataclass(frozen=True, slots=True)
class Cliente:
    """Un cliente nace con su `sub` (ingreso con token) o con un `registro_id` (alta previa).

    En el alta previa la cuenta de Cognito todavía no existe: `sub` queda vacío hasta el
    primer ingreso, cuando se vincula.
    """

    id: UUID
    sub: str | None = None
    estado: EstadoIdentidad = EstadoIdentidad.PENDIENTE
    verificado_en: datetime | None = None
    registro_id: UUID | None = None
    correo: str | None = None

    @property
    def verificado(self) -> bool:
        return self.estado is EstadoIdentidad.VERIFICADO

    def con_resultado(self, resultado: ResultadoKyc, ahora: datetime) -> "Cliente":
        """Aplica el veredicto del proveedor.

        Una captura inválida (AC2) no cambia el estado: el cliente sigue pendiente y puede
        repetir la captura, nunca queda rechazado por una selfie mal tomada.
        """
        if resultado is ResultadoKyc.APROBADO:
            return replace(self, estado=EstadoIdentidad.VERIFICADO, verificado_en=ahora)
        if resultado is ResultadoKyc.RECHAZADO:
            return replace(self, estado=EstadoIdentidad.RECHAZADO, verificado_en=None)
        return self


@dataclass(frozen=True, slots=True)
class ResultadoVerificacion:
    cliente: Cliente
    resultado: ResultadoKyc
    motivo: str | None


class ClienteNoEncontrado(Exception):
    """El cliente no existe."""


class RegistroConCorreoDistinto(Exception):
    """El registro ya se verificó con otro correo: la verificación va ligada a un correo."""


class RegistroYaVinculado(Exception):
    """La cuenta de este registro ya existe, o el `sub` ya pertenece a otro cliente."""


class TokenRegistroInvalido(Exception):
    """El token de registro no sirve. `motivo` nunca incluye el token ni sus datos."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


class IdentidadNoDisponible(Exception):
    """La verificación no terminó: el proveedor no respondió o falló (AC3, recuperable)."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo
