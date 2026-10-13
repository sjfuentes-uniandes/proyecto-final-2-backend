"""Socio de distribución y su credencial (docs/arquitectura/modelo_dominio.puml, HU-W01).

El secreto lo genera Cognito y solo viaja en CredencialesEmitidas, que se
entregan una vez al administrador y nunca se persisten ni se registran.
"""

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum


class Permiso(StrEnum):
    """Alcances del servidor de recursos de Cognito (infra: partner_scopes)."""

    COTIZAR = "cotizar"
    EMITIR = "emitir"
    CONSULTAR = "consultar"
    SINIESTROS = "siniestros"


class Estado(StrEnum):
    ACTIVO = "activo"
    DESACTIVADO = "desactivado"


class CanalVenta(StrEnum):
    PLATAFORMA_DIGITAL = "plataforma_digital"
    ENTIDAD_FINANCIERA = "entidad_financiera"
    COMERCIO = "comercio"
    OTRO = "otro"


class Plan(StrEnum):
    """Límite de solicitudes: plan de uso de API Gateway (infra: partner_tiers)."""

    BASICO = "basico"
    ESTANDAR = "estandar"


_PATRON_ID = re.compile(r"SOC-\d{6}")


class SocioNoEncontrado(Exception):
    pass


def identificador(secuencia: int) -> str:
    if not 1 <= secuencia <= 999_999:
        raise ValueError("Secuencia fuera de rango")
    return f"SOC-{secuencia:06d}"


def es_identificador(valor: str) -> bool:
    return _PATRON_ID.fullmatch(valor) is not None


@dataclass(frozen=True)
class NuevoSocio:
    nombre: str
    canal: CanalVenta
    plan: Plan
    permisos: frozenset[Permiso]

    def __post_init__(self) -> None:
        if not 2 <= len(self.nombre.strip()) <= 120:
            raise ValueError("La razón social debe tener entre 2 y 120 caracteres")
        if not self.permisos:
            raise ValueError("Seleccione al menos un permiso")
        object.__setattr__(self, "nombre", self.nombre.strip())


@dataclass(frozen=True)
class CredencialesEmitidas:
    """Se entregan una sola vez. repr oculta los valores para que no lleguen a logs."""

    client_id: str
    client_secret: str = field(repr=False)
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class Socio:
    id: str
    nombre: str
    canal: CanalVenta
    plan: Plan
    permisos: frozenset[Permiso]
    client_id: str
    api_key_id: str
    estado: Estado = Estado.ACTIVO
    creado: datetime = field(default_factory=lambda: datetime.now(UTC))
    desactivado: datetime | None = None

    @property
    def activo(self) -> bool:
        return self.estado is Estado.ACTIVO

    def desactivar(self, ahora: datetime | None = None) -> "Socio":
        """Idempotente: un socio ya desactivado conserva su fecha original."""
        if not self.activo:
            return self
        return replace(self, estado=Estado.DESACTIVADO, desactivado=ahora or datetime.now(UTC))

    def autentica(self, client_id: str, api_key_id: str | None) -> bool:
        """El token y la API key deben ser del mismo socio y el socio debe estar activo."""
        return self.activo and client_id == self.client_id and api_key_id == self.api_key_id


def permisos_de_alcances(alcances: str | None, servidor: str) -> frozenset[Permiso]:
    """Convierte el claim scope ("solventa/cotizar solventa/emitir") en permisos conocidos."""
    validos = {p.value for p in Permiso}
    prefijo = f"{servidor}/"
    return frozenset(
        Permiso(a.removeprefix(prefijo))
        for a in (alcances or "").split()
        if a.startswith(prefijo) and a.removeprefix(prefijo) in validos
    )
