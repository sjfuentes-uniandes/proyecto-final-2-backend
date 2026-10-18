"""Caso de uso: verificar la identidad de un cliente por sus datos de documento, sin biometría."""

from uuid import UUID

from solventa_common.resiliencia import DependenciaNoDisponible, Proteccion, TiempoAgotado
from solventa_common.telemetry import RESULTADO_ERROR, RESULTADO_TIMEOUT, operacion

from adaptador_identidad.application.verificaciones.puertos import PuertoProveedorDocumental
from adaptador_identidad.domain.verificaciones.modelos import (
    DatosDocumento,
    RespuestaInvalida,
    VerificacionDocumento,
)


class VerificarDocumento:
    def __init__(
        self, proveedor: PuertoProveedorDocumental, proteccion: Proteccion, timeout_ms: int
    ) -> None:
        self._proveedor = proveedor
        self._proteccion = proteccion
        self._timeout_ms = timeout_ms

    async def ejecutar(
        self, cliente_id: UUID, datos: DatosDocumento, deadline_ms: int | None
    ) -> VerificacionDocumento:
        async def llamada(timeout_s: float) -> VerificacionDocumento:
            return await self._proveedor.verificar_documento(cliente_id, datos, timeout_s)

        with operacion("verificacion.kyc_documento") as op:
            try:
                return await self._proteccion.ejecutar(
                    clave="kyc",
                    grupo="kyc",
                    llamada=llamada,
                    configurado_ms=self._timeout_ms,
                    deadline_ms=deadline_ms,
                )
            except TiempoAgotado:
                op.marcar(RESULTADO_TIMEOUT)
                raise
            except (DependenciaNoDisponible, RespuestaInvalida):
                op.marcar(RESULTADO_ERROR)
                raise
