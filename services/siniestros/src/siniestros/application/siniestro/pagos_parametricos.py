"""Estado de pago de los siniestros paramétricos por lote (HU-W22, H09).

`GET /v1/siniestros/parametricos/pagos?siniestroIds=`: lo llama `bff-web` para completar «Últimos pagos
activados». Una sola consulta de lectura (sin transacción de escritura, sin Outbox): el estado es el
derivado de `pago_de` (PENDIENTE mientras `dinero` no informa un resultado). Los ids inexistentes o de
otro origen se omiten.
"""

from collections.abc import Sequence
from uuid import UUID

from siniestros.application.siniestro.puertos import SiniestrosRepositorio
from siniestros.application.siniestro.vistas import PagoParametrico

MAX_IDS = 20


class ConsultarPagosParametricos:
    def __init__(self, repo: SiniestrosRepositorio) -> None:
        self._repo = repo

    async def ejecutar(self, siniestro_ids: Sequence[UUID]) -> tuple[PagoParametrico, ...]:
        unicos = tuple(dict.fromkeys(siniestro_ids))[:MAX_IDS]
        if not unicos:
            return ()
        return await self._repo.pagos_parametricos(unicos)
