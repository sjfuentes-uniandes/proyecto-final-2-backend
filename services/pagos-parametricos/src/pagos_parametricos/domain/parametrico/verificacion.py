"""Verificación antes de reprocesar un mensaje de la DLQ (H11; HU-W21, W21-B12): función pura sobre
la copia local de coberturas, sin llamadas a otros servicios.

Responde lo que el portal muestra al analista (`descarte.modelo.ts`, `Verificacion`):

- `coberturaActiva`: hay una cobertura `ACTIVA` del tipo y la zona del evento (o la de la póliza
  nombrada);
- `polizaVigente`: alguna de esas coberturas tiene su ventana `[vigente_desde, vigente_hasta)` sobre
  el instante observado;
- `pagoPrevio`: este evento ya tiene un siniestro (W22 llena `activaciones.siniestro_id`). Una
  activación todavía `PENDIENTE` no es un pago: reprocesar no lo duplicaría (clave única
  `(evento_id, poliza_id)`);
- `umbral` y `valorIndemnizar`: los de la cobertura que aplica (texto decimal); solo si hay una.

Si el mensaje no dice de qué tipo de evento ni de qué zona es, no hay nada que verificar (None).
"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from pagos_parametricos.domain.parametrico.modelos import Cobertura, EstadoCobertura


def _vigente(cobertura: Cobertura, instante: datetime) -> bool:
    regla = cobertura.regla
    return (
        cobertura.estado is EstadoCobertura.ACTIVA
        and regla is not None
        and regla.vigente_desde <= instante < regla.vigente_hasta
    )


def _umbral(valor: Decimal) -> str:
    """Sin ceros de más: la base guarda `120.000`, el contrato dice `120`."""
    return format(valor.normalize(), "f")


def _monto(valor: Decimal) -> str:
    """Con dos decimales, como `valorPago` (`1400000.00`)."""
    return format(valor.quantize(Decimal("0.01")), "f")


def verificar(
    candidatas: Iterable[Cobertura], observado_en: datetime, pago_previo: bool
) -> Mapping[str, Any]:
    """La verificación de un mensaje cuyas coberturas candidatas ya se buscaron."""
    ordenadas = sorted(candidatas, key=lambda c: str(c.poliza_id))
    activas = [c for c in ordenadas if c.estado is EstadoCobertura.ACTIVA and c.regla]
    vigentes = [c for c in activas if _vigente(c, observado_en)]
    verificacion: dict[str, Any] = {
        "polizaVigente": bool(vigentes),
        "coberturaActiva": bool(activas),
        "pagoPrevio": pago_previo,
    }
    aplica = next(iter(vigentes or activas), None)
    if aplica is not None and aplica.regla is not None:
        verificacion["umbral"] = _umbral(aplica.regla.umbral)
        verificacion["valorIndemnizar"] = _monto(aplica.regla.valor_pago)
    return verificacion
