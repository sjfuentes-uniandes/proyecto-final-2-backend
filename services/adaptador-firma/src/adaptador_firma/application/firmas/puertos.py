"""Puerto de salida hacia el proveedor de firma."""

from typing import Protocol

from adaptador_firma.domain.firmas.modelos import Firma, SolicitudFirma


class PuertoProveedorFirma(Protocol):
    async def firmar(self, solicitud: SolicitudFirma, timeout_s: float) -> Firma: ...
