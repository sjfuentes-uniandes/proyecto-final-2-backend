"""Tabla de decisión de HU-W03: ¿se puede cotizar este producto en esta fecha?

| # | Producto | Estado | Fecha en [desde, hasta) | Reglas vigentes en la fecha | Resultado |
| - | -------- | ------ | ----------------------- | --------------------------- | --------- |
| 1 | No existe | — | — | — | PRODUCTO_NO_ENCONTRADO |
| 2 | Existe | INACTIVO | — | — | PRODUCTO_INACTIVO |
| 3 | Existe | ACTIVO | No | — | PRODUCTO_FUERA_DE_VIGENCIA |
| 4 | Existe | ACTIVO | Sí | No | REGLAS_NO_VIGENTES |
| 5 | Existe | ACTIVO | Sí | Sí | Se calcula la prima |

Las condiciones se evalúan en ese orden: la primera que falla es la causa (una sola, estable).
"""

from datetime import datetime

from cotizacion.domain.cotizacion.errores import (
    ProductoFueraDeVigencia,
    ProductoInactivo,
    ProductoNoEncontrado,
    ReglasNoVigentes,
)
from cotizacion.domain.cotizacion.modelos import EstadoProducto, Producto, ReglasPrima


def vigente_en(producto: Producto, en: datetime) -> bool:
    """Vigencia semiabierta: incluye vigente_desde y excluye vigente_hasta."""
    return producto.vigente_desde <= en and (
        producto.vigente_hasta is None or en < producto.vigente_hasta
    )


def verificar_producto(producto: Producto | None, en: datetime) -> Producto:
    """Filas 1 a 3 de la tabla."""
    if producto is None:
        raise ProductoNoEncontrado()
    if producto.estado is not EstadoProducto.ACTIVO:
        raise ProductoInactivo()
    if not vigente_en(producto, en):
        raise ProductoFueraDeVigencia()
    return producto


def verificar_reglas(reglas: ReglasPrima | None, en: datetime) -> ReglasPrima:
    """Fila 4 de la tabla (solo tiene sentido con un producto que pasó las filas 1 a 3)."""
    if reglas is None or reglas.vigente_desde > en:
        raise ReglasNoVigentes()
    return reglas


def verificar_elegibilidad(
    producto: Producto | None, reglas: ReglasPrima | None, en: datetime
) -> tuple[Producto, ReglasPrima]:
    """Devuelve el producto y sus reglas si se puede cotizar en `en`; si no, lanza la causa."""
    return verificar_producto(producto, en), verificar_reglas(reglas, en)


def coherentes(producto: Producto, reglas: ReglasPrima) -> bool:
    """Las reglas son del producto y usan su moneda (catalogo lo valida al publicarlas)."""
    return reglas.producto_id == producto.id and reglas.moneda == producto.moneda
