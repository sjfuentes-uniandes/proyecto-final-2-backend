"""Línea de tiempo del «Estado del trámite» de H03 (HU-W19). Se deriva del estado y del historial
inmutable (DS3-14); no se guarda.

Cinco pasos, siempre en este orden: `RECIBIDO`, `EVIDENCIAS_VALIDADAS`, `EN_EVALUACION`,
`DECISION` y `PAGO_INDEMNIZACION`.

- `COMPLETADO`: el paso ocurrió y el caso ya pasó a otro.
- `ACTUAL`: donde está el caso. En `EN_EVALUACION` y `FALTA_SOPORTE` es el paso `EN_EVALUACION`
  (con la fecha y el nombre de quien tomó el caso); en los demás estados, el primer paso sin ocurrir.
- `PENDIENTE`: los que faltan.

W20 (decisión) y W23 (pago) completan los pasos `DECISION` y `PAGO_INDEMNIZACION` con sus propias
acciones del historial; aquí `DECISION` se da por hecho cuando el estado es `APROBADO` o `RECHAZADO`
y `PAGO_INDEMNIZACION` queda pendiente.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from siniestros.domain.siniestro.modelos import (
    ESTADOS_CON_ANALISTA,
    ESTADOS_DECIDIDOS,
    EntradaHistorial,
    EstadoPaso,
    EstadoSiniestro,
    PasoTramite,
)

ACCION_REPORTADO = "REPORTADO"
ACCION_EVIDENCIAS_VALIDADAS = "EVIDENCIAS_VALIDADAS"
ACCION_ASIGNADO = "ASIGNADO"

_PASO_DE_ACCION = {
    ACCION_REPORTADO: PasoTramite.RECIBIDO,
    ACCION_EVIDENCIAS_VALIDADAS: PasoTramite.EVIDENCIAS_VALIDADAS,
    ACCION_ASIGNADO: PasoTramite.EN_EVALUACION,
}


@dataclass(frozen=True)
class PasoDeTramite:
    paso: PasoTramite
    estado: EstadoPaso
    ocurrido_en: datetime | None
    actor_nombre: str | None


def pasos_tramite(
    estado: EstadoSiniestro, historial: Sequence[EntradaHistorial]
) -> tuple[PasoDeTramite, ...]:
    """Los cinco pasos del trámite para un caso en `estado` con ese `historial`."""
    ocurridos: dict[PasoTramite, EntradaHistorial] = {}
    for entrada in sorted(historial, key=lambda e: e.ocurrido_en):
        paso = _PASO_DE_ACCION.get(entrada.accion)
        if paso is not None:
            # La primera vez que ocurrió: un reintento no mueve la fecha del paso.
            ocurridos.setdefault(paso, entrada)
        if entrada.estado_nuevo in ESTADOS_DECIDIDOS:
            ocurridos.setdefault(PasoTramite.DECISION, entrada)

    pasos = list(PasoTramite)
    actual: PasoTramite | None
    if estado in ESTADOS_CON_ANALISTA:
        actual = PasoTramite.EN_EVALUACION
    else:
        actual = next((p for p in pasos if p not in ocurridos), None)

    resultado: list[PasoDeTramite] = []
    for orden in pasos:
        ocurrio = ocurridos.get(orden)
        if orden is actual:
            situacion = EstadoPaso.ACTUAL
        elif ocurrio is not None:
            situacion = EstadoPaso.COMPLETADO
        else:
            situacion = EstadoPaso.PENDIENTE
        resultado.append(
            PasoDeTramite(
                paso=orden,
                estado=situacion,
                ocurrido_en=ocurrio.ocurrido_en if ocurrio else None,
                actor_nombre=ocurrio.actor_nombre if ocurrio else None,
            )
        )
    return tuple(resultado)
