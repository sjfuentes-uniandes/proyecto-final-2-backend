"""Selección pura de las indemnizaciones PENDIENTE que una conciliación puede tomar (DS3-18).

Es la regla que la consulta SQL de `IndemnizacionesPg.reclamar` repite con `FOR UPDATE SKIP LOCKED`;
una prueba de módulo compara ambas. Una indemnización es reclamable si:

- está `PENDIENTE`;
- su última actividad (`actualizado_en`) es anterior a `ahora - antiguedad_min`: así no compite con
  un pago en curso ni con una conciliación reciente; y
- no la tiene reclamada otra réplica: `reclamado_hasta` es nulo o ya venció.

Se atienden las más antiguas primero, con `indemnizacion_id` como desempate estable.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta

from dinero.domain.indemnizacion.modelos import EstadoIndemnizacion, Indemnizacion


def es_reclamable(indemnizacion: Indemnizacion, ahora: datetime, antiguedad_min: timedelta) -> bool:
    if indemnizacion.estado is not EstadoIndemnizacion.PENDIENTE:
        return False
    if indemnizacion.actualizado_en >= ahora - antiguedad_min:
        return False
    reclamo = indemnizacion.reclamado_hasta
    return reclamo is None or reclamo < ahora


def seleccionar_pendientes(
    indemnizaciones: Iterable[Indemnizacion], ahora: datetime, antiguedad_min: timedelta, lote: int
) -> list[Indemnizacion]:
    """Hasta `lote` indemnizaciones reclamables, las más antiguas primero."""
    elegibles = (i for i in indemnizaciones if es_reclamable(i, ahora, antiguedad_min))
    ordenadas = sorted(elegibles, key=lambda i: (i.actualizado_en, str(i.indemnizacion_id)))
    return ordenadas[: max(lote, 0)]
