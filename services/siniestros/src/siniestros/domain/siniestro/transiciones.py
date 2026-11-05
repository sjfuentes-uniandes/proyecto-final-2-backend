"""Cambios de estado del siniestro (HU-W19, DS3-02). Puro.

W19 solo ejecuta `REPORTADO → EN_EVALUACION` (tomar el siguiente de la cola). El resto de la tabla
de transiciones lo agregan W20 (decisión, falta de soporte) y W22 (paramétricos). **Abrir un caso
no lo asigna.**
"""

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from siniestros.domain.siniestro.errores import SiniestroInvalido, TransicionInvalida
from siniestros.domain.siniestro.modelos import EstadoSiniestro, Siniestro, con_zona


def asignar(siniestro: Siniestro, analista_id: UUID, ahora: datetime) -> Siniestro:
    """El analista toma el caso: pasa a `EN_EVALUACION` con su analista y la hora de asignación."""
    if siniestro.estado is not EstadoSiniestro.REPORTADO:
        raise TransicionInvalida(siniestro.estado, EstadoSiniestro.EN_EVALUACION)
    if not con_zona(ahora):
        raise SiniestroInvalido("ahora: debe traer zona horaria")
    return replace(
        siniestro,
        estado=EstadoSiniestro.EN_EVALUACION,
        analista_id=analista_id,
        asignado_en=ahora,
        version=siniestro.version + 1,
    )


def codigo_siniestro(anio: int, consecutivo: int) -> str:
    """`88-AAAA-NNNN` (DS3-11): el consecutivo sale de `secuencia_codigo`, por año. Lleva al menos
    cuatro dígitos; pasado el 9999 sigue creciendo."""
    if not 2000 <= anio <= 9999 or consecutivo < 1:
        raise SiniestroInvalido("codigo: año o consecutivo fuera de rango")
    return f"88-{anio:04d}-{consecutivo:04d}"
