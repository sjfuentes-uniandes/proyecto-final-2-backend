"""Identidad del cliente. Puro: sin E/S."""

from dataclasses import dataclass, replace
from datetime import date, datetime
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
class DatosCliente:
    """Datos con los que un asesor da de alta al cliente (HU-W30). Es PII: nunca va a logs."""

    tipo_documento: str
    numero_documento: str
    nombre_completo: str
    fecha_nacimiento: date


@dataclass(frozen=True, slots=True)
class Cliente:
    """Un cliente nace con su `sub` (ingreso con token), con un `registro_id` (alta previa) o con
    su documento (alta por un asesor).

    En el alta previa la cuenta de Cognito todavía no existe: `sub` queda vacío hasta el
    primer ingreso, cuando se vincula.
    """

    id: UUID
    sub: str | None = None
    estado: EstadoIdentidad = EstadoIdentidad.PENDIENTE
    verificado_en: datetime | None = None
    registro_id: UUID | None = None
    correo: str | None = None
    tipo_documento: str | None = None
    numero_documento: str | None = None
    nombre_completo: str | None = None
    fecha_nacimiento: date | None = None
    proveedor: str | None = None
    referencia: str | None = None

    @property
    def verificado(self) -> bool:
        return self.estado is EstadoIdentidad.VERIFICADO

    @property
    def datos(self) -> DatosCliente | None:
        """Los datos del alta por documento, o `None` si el cliente nació de otra forma."""
        if (
            self.tipo_documento is None
            or self.numero_documento is None
            or self.nombre_completo is None
            or self.fecha_nacimiento is None
        ):
            return None
        return DatosCliente(
            self.tipo_documento,
            self.numero_documento,
            self.nombre_completo,
            self.fecha_nacimiento,
        )

    def con_resultado(
        self,
        resultado: ResultadoKyc,
        ahora: datetime,
        proveedor: str | None = None,
        referencia: str | None = None,
    ) -> "Cliente":
        """Aplica el veredicto del proveedor.

        Una captura inválida (AC2) no cambia el estado: el cliente sigue pendiente y puede
        repetir la captura, nunca queda rechazado por una selfie mal tomada. `proveedor` y
        `referencia` dejan la traza de la decisión; nunca la evidencia biométrica.
        """
        if resultado is ResultadoKyc.APROBADO:
            return replace(
                self,
                estado=EstadoIdentidad.VERIFICADO,
                verificado_en=ahora,
                proveedor=proveedor,
                referencia=referencia,
            )
        if resultado is ResultadoKyc.RECHAZADO:
            return replace(
                self,
                estado=EstadoIdentidad.RECHAZADO,
                verificado_en=None,
                proveedor=proveedor,
                referencia=referencia,
            )
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


class ClienteSinDocumento(Exception):
    """El cliente no nació de un alta por documento: no hay datos que enviar al proveedor."""


class IdentidadNoDisponible(Exception):
    """La verificación no terminó: el proveedor no respondió o falló (AC3, recuperable)."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo
