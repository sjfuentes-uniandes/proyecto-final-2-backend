"""Vigencia de la oferta (HU-W13, DS2-20). Pura: sin reloj ni E/S.

Desde HU-W04 la vigencia la fijan las reglas de prima (`oferta.vigenciaMinutos`) y se guarda con la
oferta (`domain/cotizacion/oferta.py`). Esta función queda como respaldo para las reglas sin
condiciones de oferta y para las cotizaciones anteriores a la migración 004: vencen
`vigencia_horas` después de calculadas (`calculadoEn + vigencia_horas`), derivado al leer. Vigente
si `ahora < vigenteHasta`: el mismo criterio semiabierto que la vigencia del producto (HU-W03).
"""

from datetime import datetime, timedelta

VIGENCIA_HORAS_POR_OMISION = 96


def vigente_hasta(calculado_en: datetime, vigencia_horas: int) -> datetime:
    """Fin de la vigencia de la oferta, en la zona de `calculado_en` (UTC en la base)."""
    return calculado_en + timedelta(hours=vigencia_horas)


def oferta_vigente(hasta: datetime, ahora: datetime) -> bool:
    """El instante exacto `hasta` ya es vencido."""
    return ahora < hasta
