"""Casos de uso del alta de un cliente por un asesor y su verificación documental (HU-W30)."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from clientes.application.identidad.puertos import (
    ConsultaVerificacionDocumental,
    RepositorioClientes,
)
from clientes.domain.identidad.modelos import (
    Cliente,
    ClienteNoEncontrado,
    ClienteSinDocumento,
    DatosCliente,
    IdentidadNoDisponible,
    ResultadoKyc,
    ResultadoVerificacion,
)

logger = logging.getLogger("clientes.identidad")


def _ahora() -> datetime:
    return datetime.now(UTC)


class CrearCliente:
    """Da de alta al cliente en PENDIENTE; el mismo documento nunca crea un segundo cliente."""

    def __init__(self, repositorio: RepositorioClientes) -> None:
        self._repositorio = repositorio

    async def ejecutar(self, datos: DatosCliente) -> tuple[Cliente, bool]:
        cliente, creado = await asyncio.to_thread(self._repositorio.asegurar_por_documento, datos)
        # Solo metadatos: el documento, el nombre y la fecha de nacimiento son PII.
        logger.info(
            "cliente dado de alta" if creado else "cliente ya existente",
            extra={"cliente_id": str(cliente.id)},
        )
        return cliente, creado


class VerificarDocumento:
    """Pide al proveedor que confirme la identidad y aplica su veredicto al cliente."""

    def __init__(
        self,
        repositorio: RepositorioClientes,
        verificacion: ConsultaVerificacionDocumental,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._repositorio = repositorio
        self._verificacion = verificacion
        self._reloj = reloj

    async def ejecutar(self, cliente_id: UUID) -> ResultadoVerificacion:
        """Una falla técnica del proveedor no cambia el estado: el cliente sigue como estaba y
        se puede reintentar. Un cliente ya VERIFICADO no vuelve a consultar al proveedor.
        """
        cliente = await asyncio.to_thread(self._repositorio.obtener, cliente_id)
        if cliente is None:
            raise ClienteNoEncontrado()
        with operacion("identidad.verificar_documento") as op:
            if cliente.verificado:
                return ResultadoVerificacion(cliente, ResultadoKyc.APROBADO, None)
            datos = cliente.datos
            if datos is None:
                raise ClienteSinDocumento()
            try:
                veredicto = await self._verificacion.verificar_documento(cliente.id, datos)
            except IdentidadNoDisponible as error:
                op.marcar(
                    RESULTADO_TIMEOUT if error.motivo == "TIEMPO_AGOTADO" else RESULTADO_ERROR
                )
                raise
            actualizado = cliente.con_resultado(
                veredicto.resultado, self._reloj(), veredicto.proveedor, veredicto.referencia
            )
            if actualizado != cliente:
                await asyncio.to_thread(self._repositorio.guardar, actualizado)
            if veredicto.resultado is not ResultadoKyc.APROBADO:
                op.marcar(RESULTADO_RECHAZADO)
            logger.info(
                "verificación documental procesada",
                extra={
                    "cliente_id": str(cliente.id),
                    "resultado": str(veredicto.resultado),
                    "proveedor": veredicto.proveedor,
                    "referencia": veredicto.referencia,
                },
            )
        return ResultadoVerificacion(actualizado, veredicto.resultado, veredicto.motivo)
