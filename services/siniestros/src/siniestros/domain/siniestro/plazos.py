"""Reloj del compromiso de 48 h y prioridad derivada (HU-W19, DS3-04). Puro: la hora llega como
argumento (`ahora`); el dominio no la lee.

    transcurrido = (corte − reportado_en) − pausa_acumulada − (corte − pausado_desde si está pausado)
    tiempo restante (min) = floor((compromiso − transcurrido) / 60 s)        # negativo si venció

`corte` es `ahora`, salvo en un caso decidido: ahí el reloj se detuvo en `decidido_en`.
"""

from datetime import datetime, timedelta

from siniestros.domain.siniestro.errores import SiniestroInvalido
from siniestros.domain.siniestro.modelos import PrioridadSiniestro, Siniestro, con_zona

COMPROMISO_HORAS = 48
# DS3-04: ALTA si queda menos de 8 h (o venció); MEDIA si queda menos de 24 h; BAJA en otro caso.
UMBRAL_ALTA_MIN = 8 * 60
UMBRAL_MEDIA_MIN = 24 * 60

_MINUTO = timedelta(minutes=1)


def tiempo_restante(
    siniestro: Siniestro, ahora: datetime, *, compromiso_horas: int = COMPROMISO_HORAS
) -> int:
    """Minutos que faltan para el compromiso; negativo si está vencido (se redondea hacia abajo)."""
    if not con_zona(ahora):
        raise SiniestroInvalido("ahora: debe traer zona horaria")
    corte = siniestro.decidido_en or ahora
    transcurrido = corte - siniestro.reportado_en - timedelta(seconds=siniestro.pausa_acumulada_s)
    if siniestro.pausado_desde is not None:
        transcurrido -= corte - siniestro.pausado_desde
    # Un reloj atrasado (corte antes del reporte) no suma tiempo: el compromiso queda completo.
    transcurrido = max(transcurrido, timedelta(0))
    return (timedelta(hours=compromiso_horas) - transcurrido) // _MINUTO


def prioridad(
    tiempo_restante_min: int,
    *,
    alta_min: int = UMBRAL_ALTA_MIN,
    media_min: int = UMBRAL_MEDIA_MIN,
) -> PrioridadSiniestro:
    """Se deriva del tiempo restante, no se captura. Los límites son estrictos: justo `alta_min`
    es MEDIA y justo `media_min` es BAJA."""
    if tiempo_restante_min < alta_min:
        return PrioridadSiniestro.ALTA
    if tiempo_restante_min < media_min:
        return PrioridadSiniestro.MEDIA
    return PrioridadSiniestro.BAJA
