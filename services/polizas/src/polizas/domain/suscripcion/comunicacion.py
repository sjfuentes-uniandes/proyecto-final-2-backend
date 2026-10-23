"""Comunicación de la decisión de suscripción (HU-W14, W14-B01). Pura: el instante lo recibe.

La condición, si admite contratación y el siguiente paso salen de una tabla por resultado y
vigencia de la oferta; la vigencia solo distingue a la aprobada (DS2-10):

| Resultado          | Oferta  | Condición            | Contratación | Siguiente paso      |
| ------------------ | ------- | -------------------- | ------------ | ------------------- |
| APROBADA           | vigente | APROBADA_SIN_AJUSTES | sí           | ACEPTAR_OFERTA      |
| APROBADA           | vencida | APROBADA_SIN_AJUSTES | no           | COTIZAR_NUEVAMENTE  |
| RECHAZADA          | ambas   | NO_ASEGURABLE        | no           | COTIZAR_ALTERNATIVA |
| REVISION_REQUERIDA | ambas   | EN_REVISION          | no           | ESPERAR_REVISION    |

Datos adicionales: la aprobada lleva `vigenteHasta`; el rechazo, el motivo de la regla
determinante (`NO_ELEGIBLE` si es RESERVADA, DS2-07) y las alternativas de la versión de reglas;
la revisión, el plazo de respuesta (compromiso de polizas, R-04) y el motivo de la primera regla
EXPLICABLE que no se cumple o no es evaluable (R-12), o ninguno. Una revisión nunca usa
`NO_ELEGIBLE` y la comunicación nunca lleva el código ni el valor de una regla.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from polizas.domain.suscripcion.modelos import (
    Alternativa,
    Comunicacion,
    CondicionAseguramiento,
    Decision,
    Evaluacion,
    ResultadoDecision,
    ResultadoRegla,
    SiguientePaso,
    Visibilidad,
)
from polizas.domain.suscripcion.vigencia import oferta_vigente

MOTIVO_NO_ELEGIBLE = "NO_ELEGIBLE"


@dataclass(frozen=True, slots=True)
class FilaComunicacion:
    condicion: CondicionAseguramiento
    admite_contratacion: bool
    siguiente_paso: SiguientePaso


_APROBADA = FilaComunicacion(
    CondicionAseguramiento.APROBADA_SIN_AJUSTES, True, SiguientePaso.ACEPTAR_OFERTA
)
_APROBADA_VENCIDA = FilaComunicacion(
    CondicionAseguramiento.APROBADA_SIN_AJUSTES, False, SiguientePaso.COTIZAR_NUEVAMENTE
)
_RECHAZADA = FilaComunicacion(
    CondicionAseguramiento.NO_ASEGURABLE, False, SiguientePaso.COTIZAR_ALTERNATIVA
)
_EN_REVISION = FilaComunicacion(
    CondicionAseguramiento.EN_REVISION, False, SiguientePaso.ESPERAR_REVISION
)

# (resultado, oferta vigente) → fila.
TABLA: Mapping[tuple[ResultadoDecision, bool], FilaComunicacion] = {
    (ResultadoDecision.APROBADA, True): _APROBADA,
    (ResultadoDecision.APROBADA, False): _APROBADA_VENCIDA,
    (ResultadoDecision.RECHAZADA, True): _RECHAZADA,
    (ResultadoDecision.RECHAZADA, False): _RECHAZADA,
    (ResultadoDecision.REVISION_REQUERIDA, True): _EN_REVISION,
    (ResultadoDecision.REVISION_REQUERIDA, False): _EN_REVISION,
}


def motivo_rechazo(evaluacion: Evaluacion) -> str:
    """El motivo de la regla determinante; `NO_ELEGIBLE` si es RESERVADA o no se encuentra."""
    determinante = next(
        (r.regla for r in evaluacion.reglas if r.regla.codigo == evaluacion.regla_determinante),
        None,
    )
    if determinante is None or determinante.visibilidad is Visibilidad.RESERVADA:
        return MOTIVO_NO_ELEGIBLE
    return determinante.motivo_cliente


def motivo_revision(evaluacion: Evaluacion) -> str | None:
    """R-12: el de la primera regla EXPLICABLE (por `orden`) que no se cumple o no es evaluable.
    Si solo fallan reglas RESERVADA, None (el portal usa un texto genérico)."""
    pendientes = (
        r.regla
        for r in sorted(evaluacion.reglas, key=lambda r: r.regla.orden)
        if r.resultado is not ResultadoRegla.CUMPLE
        and r.regla.visibilidad is Visibilidad.EXPLICABLE
        and r.regla.motivo_cliente != MOTIVO_NO_ELEGIBLE
    )
    return next((regla.motivo_cliente for regla in pendientes), None)


def comunicar(
    decision: Decision,
    alternativas: Sequence[Alternativa],
    ahora: datetime,
    compromiso_revision_horas: int,
) -> Comunicacion:
    """La comunicación de la decisión en el instante `ahora`. `alternativas` son las de la versión
    de reglas con que se decidió; solo se comunican en un rechazo."""
    vigente_hasta = decision.insumos.vigente_hasta
    fila = TABLA[(decision.resultado, oferta_vigente(vigente_hasta, ahora))]
    if decision.resultado is ResultadoDecision.APROBADA:
        return _comunicacion(fila, vigente_hasta=vigente_hasta)
    if decision.resultado is ResultadoDecision.RECHAZADA:
        return _comunicacion(
            fila,
            motivo_cliente=motivo_rechazo(decision.evaluacion),
            alternativas=tuple(alternativas),
        )
    return _comunicacion(
        fila,
        motivo_cliente=motivo_revision(decision.evaluacion),
        plazo_respuesta_horas=compromiso_revision_horas,
    )


def _comunicacion(
    fila: FilaComunicacion,
    *,
    vigente_hasta: datetime | None = None,
    motivo_cliente: str | None = None,
    alternativas: tuple[Alternativa, ...] = (),
    plazo_respuesta_horas: int | None = None,
) -> Comunicacion:
    return Comunicacion(
        condicion=fila.condicion,
        admite_contratacion=fila.admite_contratacion,
        siguiente_paso=fila.siguiente_paso,
        vigente_hasta=vigente_hasta,
        motivo_cliente=motivo_cliente,
        alternativas=alternativas,
        plazo_respuesta_horas=plazo_respuesta_horas,
    )
