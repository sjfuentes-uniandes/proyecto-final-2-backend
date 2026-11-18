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
            if poliza.estado != "ACTIVA" or not poliza.vigente:
                op.marcar(RESULTADO_RECHAZADO)
                raise PolizaNoVigente
            resultado = await self._siniestros.crear_borrador(
                sub, cliente_id, poliza, descripcion, clave
            )
            op.marcar("creado")
            return resultado


class EnviarBorrador:
    def __init__(
        self, resolutor: ResolutorCliente, consulta: ConsultaPolizas, siniestros: SiniestrosApi
    ) -> None:
        self._resolutor = resolutor
        self._consulta = consulta
        self._siniestros = siniestros

    async def ejecutar(self, sub: str, borrador_id: UUID, clave: UUID) -> dict[str, Any]:
        with operacion("siniestros.borrador.enviar"):
            borrador = await self._siniestros.consultar_borrador(sub, borrador_id)
            if borrador.get("estado") == "CONVERTIDO":
                return await self._siniestros.enviar_borrador(sub, borrador_id, clave, None)
            if borrador.get("estado") != "ABIERTO":
                raise ServicioNoDisponible("ERROR")
            try:
                poliza_id = UUID(borrador["polizaId"])
            except (KeyError, ValueError, TypeError, AttributeError) as error:
                raise ServicioNoDisponible("ERROR") from error
            cliente_id = await self._resolutor.resolver(sub)
            poliza = await self._consulta.obtener(cliente_id, poliza_id, sub)
            if poliza.estado != "ACTIVA" or not poliza.vigente:
                # Otra solicitud pudo confirmar mientras consultabamos la poliza.
                actual = await self._siniestros.consultar_borrador(sub, borrador_id)
                if actual.get("estado") == "CONVERTIDO":
                    return await self._siniestros.enviar_borrador(sub, borrador_id, clave, None)
                raise PolizaNoVigente
            return await self._siniestros.enviar_borrador(sub, borrador_id, clave, poliza)


class ActualizarBorrador:
    def __init__(self, siniestros: SiniestrosApi) -> None:
        self._siniestros = siniestros

    async def ejecutar(
        self, sub: str, borrador_id: UUID, cambios: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._siniestros.actualizar_borrador(sub, borrador_id, cambios)


class EliminarEvidencia:
    def __init__(self, siniestros: SiniestrosApi) -> None:
        self._siniestros = siniestros

    async def ejecutar(self, sub: str, borrador_id: UUID, evidencia_id: UUID) -> None:
        await self._siniestros.eliminar_evidencia(sub, borrador_id, evidencia_id)


class ListarSiniestros:
    def __init__(self, resolutor: ResolutorCliente, siniestros: SiniestrosApi) -> None:
        self._resolutor = resolutor
        self._siniestros = siniestros

    async def ejecutar(self, sub: str, limite: int, cursor: str | None) -> dict[str, Any]:
        cliente_id = await self._resolutor.resolver(sub)
        return await self._siniestros.listar_siniestros(sub, cliente_id, limite, cursor)


class ConsultarSiniestro:
    def __init__(self, resolutor: ResolutorCliente, siniestros: SiniestrosApi) -> None:
        self._resolutor = resolutor
        self._siniestros = siniestros

    async def ejecutar(self, sub: str, siniestro_id: UUID) -> dict[str, Any]:
        cliente_id = await self._resolutor.resolver(sub)
        return await self._siniestros.consultar_siniestro(sub, cliente_id, siniestro_id)


class EnlazarEvidencia:
    def __init__(self, resolutor: ResolutorCliente, siniestros: SiniestrosApi) -> None:
        self._resolutor = resolutor
        self._siniestros = siniestros

    async def ejecutar(self, sub: str, siniestro_id: UUID, evidencia_id: UUID) -> dict[str, Any]:
        cliente_id = await self._resolutor.resolver(sub)
        return await self._siniestros.enlace_evidencia(sub, cliente_id, siniestro_id, evidencia_id)


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
    "ActualizarBorrador",
    "AgregarEvidencia",
    "ConfirmarEvidencia",
    "ConsultarBorrador",
    "ConsultarLimitesEvidencia",
    "ConsultarSiniestro",
    "CrearBorrador",
    "EliminarEvidencia",
    "EnlazarEvidencia",
    "EnviarBorrador",
    "ListarBorradores",
    "ListarSiniestros",
    "RenovarCargaEvidencia",
]
