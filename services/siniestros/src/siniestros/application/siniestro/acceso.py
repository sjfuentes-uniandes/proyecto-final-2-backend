"""Auditoría de accesos denegados (HU-W19, DS3-09). Escribe `AccesoSiniestroDenegado` en el Outbox:
lo rechaza `siniestros` (rol inválido, `origen = SINIESTROS`) o lo avisa `bff-web` (usuario sin el
grupo, `origen = BFF_WEB`). El evento sale siempre del dueño del recurso."""

from solventa_common.telemetry import operacion

from siniestros.application.siniestro.puertos import Reloj, SiniestrosRepositorio
from siniestros.application.siniestro.vistas import AccesoDenegado


class RegistrarAccesoDenegado:
    def __init__(self, repo: SiniestrosRepositorio, reloj: Reloj) -> None:
        self._repo = repo
        self._reloj = reloj

    async def ejecutar(self, acceso: AccesoDenegado) -> None:
        with operacion("siniestro.acceso_denegado"):
            async with self._repo.transaccion() as tx:
                await tx.evento_acceso_denegado(acceso, self._reloj.ahora())
