"""Casos de uso de reporte móvil: validar la póliza antes de crear su borrador."""

from typing import Any
from uuid import UUID

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from bff_movil.application.polizas.puertos import ConsultaPolizas
from bff_movil.application.sesion.puertos import ResolutorCliente
from bff_movil.application.siniestros.puertos import SiniestrosApi
from bff_movil.domain.polizas.modelos import PolizaNoAutorizada, PolizaNoEncontrada
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible
from bff_movil.domain.siniestros.errores import PolizaNoVigente


class ConsultarLimitesEvidencia:
    def __init__(self, siniestros: SiniestrosApi) -> None:
        self._siniestros = siniestros

    async def ejecutar(self, sub: str) -> dict[str, Any]:
        return await self._siniestros.limites(sub)


class ListarBorradores:
    def __init__(self, siniestros: SiniestrosApi) -> None:
        self._siniestros = siniestros

    async def ejecutar(self, sub: str) -> dict[str, Any]:
        return await self._siniestros.listar_borradores(sub)


class ConsultarBorrador:
    def __init__(self, siniestros: SiniestrosApi) -> None:
        self._siniestros = siniestros

    async def ejecutar(self, sub: str, borrador_id: UUID) -> dict[str, Any]:
        return await self._siniestros.consultar_borrador(sub, borrador_id)


class CrearBorrador:
    def __init__(
        self,
        resolutor: ResolutorCliente,
        consulta: ConsultaPolizas,
        siniestros: SiniestrosApi,
    ) -> None:
        self._resolutor = resolutor
        self._consulta = consulta
        self._siniestros = siniestros

    async def ejecutar(
        self, sub: str, poliza_id: UUID, descripcion: str, clave: UUID
    ) -> dict[str, Any]:
        with operacion("siniestros.borrador.crear") as op:
            try:
                cliente_id = await self._resolutor.resolver(sub)
                poliza = await self._consulta.obtener(cliente_id, poliza_id, sub)
            except ServicioNoDisponible as error:
                op.marcar("timeout" if error.motivo == "TIEMPO_AGOTADO" else "error")
                raise
            except (ClienteNoEncontrado, PolizaNoAutorizada, PolizaNoEncontrada):
                op.marcar(RESULTADO_RECHAZADO)
                raise
            if poliza.estado != "EMITIDA" or not poliza.vigente:
                op.marcar(RESULTADO_RECHAZADO)
                raise PolizaNoVigente
            resultado = await self._siniestros.crear_borrador(
                sub, cliente_id, poliza, descripcion, clave
            )
            op.marcar("creado")
            return resultado


class AgregarEvidencia:
    def __init__(self, siniestros: SiniestrosApi) -> None:
        self._siniestros = siniestros

    async def ejecutar(
        self, sub: str, borrador_id: UUID, solicitud: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._siniestros.agregar_evidencia(sub, borrador_id, solicitud)


class ConfirmarEvidencia:
    def __init__(self, siniestros: SiniestrosApi) -> None:
        self._siniestros = siniestros

    async def ejecutar(self, sub: str, borrador_id: UUID, evidencia_id: UUID) -> dict[str, Any]:
        return await self._siniestros.confirmar_evidencia(sub, borrador_id, evidencia_id)


class RenovarCargaEvidencia:
    def __init__(self, siniestros: SiniestrosApi) -> None:
        self._siniestros = siniestros

    async def ejecutar(self, sub: str, borrador_id: UUID, evidencia_id: UUID) -> dict[str, Any]:
        return await self._siniestros.renovar_carga(sub, borrador_id, evidencia_id)


__all__ = [
    "AgregarEvidencia",
    "ConfirmarEvidencia",
    "ConsultarBorrador",
    "ConsultarLimitesEvidencia",
    "CrearBorrador",
    "ListarBorradores",
    "RenovarCargaEvidencia",
]
