"""Bandeja del analista (H01), cola general (H02) e indicadores (HU-W19, CF-S3-05 y CF-S3-06)."""

from datetime import datetime
from uuid import UUID

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from siniestros.application.siniestro.errores import SolicitudInvalida
from siniestros.application.siniestro.puertos import Reloj, SiniestrosRepositorio
from siniestros.application.siniestro.vistas import (
    FiltroBandeja,
    Indicadores,
    PaginaBandeja,
    Plazos,
    resumen_de,
)


class ListarBandeja:
    """«Mi bandeja» (`MIA`: menor tiempo restante primero) o la cola «Sin asignar» (el más antiguo
    primero), con filtros y paginación por cursor. La prioridad se deriva con el reloj inyectado."""

    def __init__(self, repo: SiniestrosRepositorio, reloj: Reloj, plazos: Plazos) -> None:
        self._repo = repo
        self._reloj = reloj
        self._plazos = plazos

    async def ejecutar(self, filtro: FiltroBandeja) -> PaginaBandeja:
        with operacion("siniestro.bandeja"):
            pagina = await self._repo.bandeja(filtro)
            ahora = self._reloj.ahora()
            return PaginaBandeja(
                items=tuple(resumen_de(s, ahora, self._plazos) for s in pagina.items),
                total=pagina.total,
                siguiente_cursor=pagina.siguiente_cursor,
            )


class ConsultarResumen:
    """Indicadores de la bandeja; `desde` y `hasta` delimitan el día del usuario."""

    def __init__(self, repo: SiniestrosRepositorio, reloj: Reloj, plazos: Plazos) -> None:
        self._repo = repo
        self._reloj = reloj
        self._plazos = plazos

    async def ejecutar(self, actor_id: UUID, desde: datetime, hasta: datetime) -> Indicadores:
        with operacion("siniestro.resumen") as op:
            if desde >= hasta:
                op.marcar(RESULTADO_RECHAZADO)
                raise SolicitudInvalida(["desde", "hasta"])
            return await self._repo.indicadores(
                actor_id, desde, hasta, self._reloj.ahora(), self._plazos.compromiso_horas
            )
