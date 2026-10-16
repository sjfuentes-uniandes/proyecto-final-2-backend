"""Caso de uso: verificar la identidad de un cliente con el proveedor KYC."""

from typing import Any
from uuid import UUID

from solventa_common.resiliencia import DependenciaNoDisponible, Proteccion, TiempoAgotado
from solventa_common.telemetry import RESULTADO_ERROR, RESULTADO_TIMEOUT, operacion

from adaptador_identidad.application.verificaciones.puertos import PuertoProveedorKyc
from adaptador_identidad.domain.verificaciones.modelos import RespuestaInvalida, Verificacion


class VerificarIdentidad:
    def __init__(
        self, proveedor: PuertoProveedorKyc, proteccion: Proteccion, timeout_ms: int
    ) -> None:
        self._proveedor = proveedor
        self._proteccion = proteccion
        self._timeout_ms = timeout_ms

    async def ejecutar(
        self,
        cliente_id: UUID,
        selfie: str,
        prueba_de_vida: dict[str, Any],
        deadline_ms: int | None,
    ) -> Verificacion:
        async def llamada(timeout_s: float) -> Verificacion:
            return await self._proveedor.verificar(cliente_id, selfie, prueba_de_vida, timeout_s)

        with operacion("verificacion.kyc") as op:
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
            except DependenciaNoDisponible:
                op.marcar(RESULTADO_ERROR)
                raise
            except RespuestaInvalida:
                op.marcar(RESULTADO_ERROR)
                raise
