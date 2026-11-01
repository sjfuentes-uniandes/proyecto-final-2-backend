"""Caso de uso: firmar la huella del documento de la póliza con el proveedor de firma (HU-W17)."""

from solventa_common.resiliencia import DependenciaNoDisponible, Proteccion, TiempoAgotado
from solventa_common.telemetry import RESULTADO_ERROR, RESULTADO_TIMEOUT, operacion

from adaptador_firma.application.firmas.puertos import PuertoProveedorFirma
from adaptador_firma.domain.firmas.modelos import Firma, RespuestaInvalida, SolicitudFirma


class Firmar:
    """Una sola llamada, sin reintentos: `polizas` reintenta con la misma clave idempotente."""

    def __init__(
        self, proveedor: PuertoProveedorFirma, proteccion: Proteccion, timeout_ms: int
    ) -> None:
        self._proveedor = proveedor
        self._proteccion = proteccion
        self._timeout_ms = timeout_ms

    async def ejecutar(self, solicitud: SolicitudFirma) -> Firma:
        async def llamada(timeout_s: float) -> Firma:
            return await self._proveedor.firmar(solicitud, timeout_s)

        # `firma.documento` es la operación de la alarma de degradación del proveedor (infra).
        with operacion("firma.documento") as op:
            try:
                return await self._proteccion.ejecutar(
                    clave="firma",
                    grupo="firma",
                    llamada=llamada,
                    configurado_ms=self._timeout_ms,
                    deadline_ms=None,
                )
            except TiempoAgotado:
                op.marcar(RESULTADO_TIMEOUT)
                raise
            except (DependenciaNoDisponible, RespuestaInvalida):
                op.marcar(RESULTADO_ERROR)
                raise
