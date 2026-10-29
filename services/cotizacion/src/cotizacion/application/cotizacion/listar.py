"""Listado de cotizaciones con oferta ("Cotizaciones abiertas" del portal del asesor).

Se pagina por cursor (creada_en, cotizacion_id), de la más reciente a la más antigua: estable aunque
lleguen cotizaciones nuevas. El cursor es opaco; uno que no entregó cotizacion es una solicitud
inválida. Un SOCIO solo ve las suyas, como en la lectura por id; un ASESOR ve todas.
"""

import base64
import binascii
import dataclasses
from datetime import datetime
from uuid import UUID

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from cotizacion.application.cotizacion.puertos import ListadoCotizaciones
from cotizacion.application.perfilamiento.puertos import Reloj
from cotizacion.domain.cotizacion.errores import CursorInvalido
from cotizacion.domain.cotizacion.listado import (
    FiltrosCotizaciones,
    PaginaCotizaciones,
    ResumenCotizacion,
)
from cotizacion.domain.cotizacion.modelos import Actor, RolActor


def codificar_cursor(resumen: ResumenCotizacion) -> str:
    crudo = f"{resumen.creada_en.isoformat()}|{resumen.cotizacion_id}"
    return base64.urlsafe_b64encode(crudo.encode()).decode()


def decodificar_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        instante, identificador = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        creada_en = datetime.fromisoformat(instante)
        cotizacion_id = UUID(identificador)
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise CursorInvalido() from error
    if creada_en.tzinfo is None:
        raise CursorInvalido()
    return creada_en, cotizacion_id


class ListarCotizaciones:
    def __init__(self, repo: ListadoCotizaciones, reloj: Reloj) -> None:
        self._repo = repo
        self.reloj = reloj

    async def ejecutar(
        self, filtros: FiltrosCotizaciones, actor: Actor, limite: int, cursor: str | None
    ) -> PaginaCotizaciones:
        if actor.rol is RolActor.SOCIO:
            filtros = dataclasses.replace(filtros, socio_id=actor.id)
        with operacion("cotizacion.listar") as op:
            try:
                despues = decodificar_cursor(cursor) if cursor else None
            except CursorInvalido:
                op.marcar(RESULTADO_RECHAZADO)
                raise
            ahora = self.reloj.ahora()
            resumenes = await self._repo.listar(filtros, ahora, limite + 1, despues)
            total = await self._repo.contar(filtros, ahora)
        pagina = tuple(resumenes[:limite])
        siguiente = codificar_cursor(pagina[-1]) if len(resumenes) > limite else None
        return PaginaCotizaciones(pagina, siguiente, total)
