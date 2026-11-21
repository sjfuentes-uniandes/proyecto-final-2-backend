"""La cobertura paramétrica de una cotización (HU-W24, sobre la regla de producto de HU-W21).

Un producto paramétrico del catálogo trae su regla (tipo de evento, umbral, unidad, valor a pagar y
versión), pero no la zona: esa es del riesgo de cada cotización (`datosAsegurado.zona`, municipio DANE
o código de vuelo). La cotización guarda la instantánea de ambas, como guarda sus coberturas, y
`polizas` la publica en `PolizaEmitida.coberturaParametrica` para la copia local de
`pagos-parametricos`. En un producto que no es paramétrico la zona no se usa.
"""

from cotizacion.domain.cotizacion.errores import ZonaRequerida
from cotizacion.domain.cotizacion.modelos import CoberturaParametrica, Producto

CAMPO_ZONA = "datosAsegurado.zona"


def cobertura_parametrica(producto: Producto, zona: str | None) -> CoberturaParametrica | None:
    """La instantánea de la regla y la zona; None si el producto no es paramétrico. Lanza
    `ZonaRequerida` si lo es y no hay zona."""
    regla = producto.parametrico
    if regla is None:
        return None
    if not zona:
        raise ZonaRequerida((CAMPO_ZONA,))
    return CoberturaParametrica(
        tipo_evento=regla.tipo_evento,
        zona=zona,
        umbral=regla.umbral,
        unidad=regla.unidad,
        valor_pago=regla.valor_pago,
        moneda=producto.moneda,
        version_regla=regla.version_regla,
    )
