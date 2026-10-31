"""Casos de uso del registro de dispositivos (HU-M11)."""

from uuid import UUID

from avisos.application.dispositivos.puertos import RepositorioDispositivos
from avisos.application.entregas.puertos import Reloj


class RegistrarDispositivo:
    def __init__(self, dispositivos: RepositorioDispositivos, reloj: Reloj) -> None:
        self._dispositivos = dispositivos
        self._reloj = reloj

    async def ejecutar(self, cliente_id: UUID, token: str) -> UUID:
        return await self._dispositivos.registrar(cliente_id, token, self._reloj.ahora())


class DarDeBajaDispositivo:
    def __init__(self, dispositivos: RepositorioDispositivos, reloj: Reloj) -> None:
        self._dispositivos = dispositivos
        self._reloj = reloj

    async def ejecutar(self, cliente_id: UUID, dispositivo_id: UUID) -> None:
        await self._dispositivos.dar_de_baja(dispositivo_id, cliente_id, self._reloj.ahora())
