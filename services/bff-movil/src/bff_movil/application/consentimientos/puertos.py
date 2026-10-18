from typing import Protocol
from uuid import UUID

from bff_movil.domain.consentimientos.modelos import (
    Consentimiento,
    Historial,
    Otorgamiento,
    SolicitudOtorgamiento,
    TextoVigente,
)


class ServicioConsentimientos(Protocol):
    """Rutas `actual` de `clientes`: el titular es siempre el `sub` del token."""

    async def otorgar(
        self, sub: str, clave: UUID, solicitud: SolicitudOtorgamiento
    ) -> Otorgamiento:
        """Lanza IdentidadNoVerificada, ClienteNoEncontrado, ConsentimientoYaVigente,
        SolicitudRechazada o ServicioNoDisponible."""
        ...

    async def listar(self, sub: str) -> list[Consentimiento]:
        """Lanza ServicioNoDisponible."""
        ...

    async def detalle(self, sub: str, consentimiento_id: UUID) -> Consentimiento:
        """Lanza ConsentimientoNoEncontrado o ServicioNoDisponible."""
        ...

    async def revocar(self, sub: str, consentimiento_id: UUID) -> Consentimiento:
        """Idempotente por estado en `clientes`: sin cuerpo ni Idempotency-Key. Devuelve el
        estado resultante (`REVOCADO`, o `VENCIDO` si ya había vencido). Lanza
        ConsentimientoNoEncontrado o ServicioNoDisponible."""
        ...

    async def historial(self, sub: str, consentimiento_id: UUID) -> Historial:
        """Lanza ConsentimientoNoEncontrado o ServicioNoDisponible."""
        ...

    async def texto_vigente(self, proposito: str | None, idioma: str | None) -> TextoVigente:
        """Lanza SolicitudRechazada o ServicioNoDisponible."""
        ...
