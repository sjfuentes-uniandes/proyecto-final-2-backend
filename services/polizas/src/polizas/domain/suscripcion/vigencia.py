"""Vigencia de la oferta (DS2-10). Pura: el instante actual lo recibe."""

from datetime import datetime


def oferta_vigente(vigente_hasta: datetime, ahora: datetime) -> bool:
    """Vigente si ahora < vigenteHasta: el instante exacto vigenteHasta ya es vencido."""
    return ahora < vigente_hasta
