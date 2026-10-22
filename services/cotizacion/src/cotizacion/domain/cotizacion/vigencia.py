"""Vigencia de la oferta (HU-W13, DS2-20). Pura: sin reloj ni E/S.

Mientras HU-W04 no defina su entidad oferta, la oferta es la cotización inmutable y vence
`vigencia_horas` después de calculada: `vigenteHasta = calculadoEn + vigencia_horas`. Se calcula al
leer (sin migración: `calculado_en` ya está guardado y no cambia), así que el resultado es
determinista para un mismo parámetro. Vigente si `ahora < vigenteHasta`: el mismo criterio
semiabierto que la vigencia del producto (HU-W03). HU-W04 puede reutilizar esta función o
reemplazarla, conservando el nombre y el significado de `vigenteHasta` en los contratos.
"""

from datetime import datetime, timedelta

VIGENCIA_HORAS_POR_OMISION = 96


def vigente_hasta(calculado_en: datetime, vigencia_horas: int) -> datetime:
    """Fin de la vigencia de la oferta, en la zona de `calculado_en` (UTC en la base)."""
    return calculado_en + timedelta(hours=vigencia_horas)


def oferta_vigente(hasta: datetime, ahora: datetime) -> bool:
    """El instante exacto `hasta` ya es vencido."""
    return ahora < hasta
