from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from clientes.domain.consentimientos.modelos import (
    Consentimiento,
    RegistroIdempotencia,
    SolicitudOtorgamiento,
)
from clientes.domain.consentimientos.textos import TextoLegal


@dataclass(frozen=True, slots=True)
class Otorgamiento:
    """Lo que `crear` persiste en una sola transacción: consentimiento, clave y evento."""

    id: UUID
    cliente_id: UUID
    solicitud: SolicitudOtorgamiento
    clave: UUID
    huella: str
    actor_id: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class Revocacion:
    """Lo que `revocar` necesita: el consentimiento del cliente y quién lo revoca."""

    consentimiento_id: UUID
    cliente_id: UUID
    actor_id: str
    correlation_id: str


class RepositorioConsentimientos(Protocol):
    """Todas las marcas de tiempo salen del reloj de la base de datos (ADR 0010)."""

    async def por_clave(self, cliente_id: UUID, clave: UUID) -> RegistroIdempotencia | None:
        """La clave del cliente si no pasaron las 24 h de retención."""
        ...

    async def activo(self, cliente_id: UUID, proposito: str) -> Consentimiento | None:
        """El `ACTIVO` almacenado, que puede estar ya vencido sin materializar."""
        ...

    async def crear(self, otorgamiento: Otorgamiento) -> Consentimiento:
        """Materializa el `ACTIVO` vencido del propósito, crea el consentimiento con
        `otorgado_en` y `vigente_hasta` de la base, registra la clave y escribe el evento en
        el outbox, todo en una transacción.

        Lanza ConsentimientoActivoExistente o ClaveIdempotenciaEnUso si otra transacción ganó.
        """
        ...

    async def obtener(self, cliente_id: UUID, consentimiento_id: UUID) -> Consentimiento | None:
        """None si no existe o es de otro cliente."""
        ...

    async def listar(self, cliente_id: UUID) -> list[Consentimiento]:
        """Del más reciente al más antiguo."""
        ...

    async def revocar(self, revocacion: Revocacion) -> Consentimiento | None:
        """Revoca en una sola sentencia si el consentimiento está `ACTIVO` y vigente en ese
        instante, y escribe el evento en el outbox en la misma transacción (ADR 0009).

        Si no lo está (ya `REVOCADO`, `VENCIDO` o `ACTIVO` vencido sin materializar), lo devuelve
        tal como está, sin cambios ni evento. None si no existe o es de otro cliente.
        """
        ...


class CatalogoTextos(Protocol):
    def vigente(self, proposito: str, idioma: str) -> TextoLegal: ...
