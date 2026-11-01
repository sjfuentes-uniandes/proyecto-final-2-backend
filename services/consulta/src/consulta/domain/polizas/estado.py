"""Estado de una póliza derivado de su vigencia (HU-W18). No se guarda: se calcula al leer."""

from datetime import date
from enum import StrEnum

from consulta.domain.polizas.modelos import Vigencia


class EstadoVigencia(StrEnum):
    NO_INICIADA = "NO_INICIADA"
    VIGENTE = "VIGENTE"
    POR_VENCER = "POR_VENCER"
    VENCIDA = "VENCIDA"


def estado_de(vigencia: Vigencia, hoy: date, dias_por_vencer: int) -> EstadoVigencia:
    """Fechas inclusivas en America/Bogota. Quedan `dias_por_vencer` días o menos: POR_VENCER."""
    if hoy < vigencia.desde:
        return EstadoVigencia.NO_INICIADA
    if hoy > vigencia.hasta:
        return EstadoVigencia.VENCIDA
    if (vigencia.hasta - hoy).days <= dias_por_vencer:
        return EstadoVigencia.POR_VENCER
    return EstadoVigencia.VIGENTE
