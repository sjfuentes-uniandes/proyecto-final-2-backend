"""Qué fuentes y campos se consultan según el consentimiento (03 §B.3 paso 2; DT-05 y DT-07)."""

from cotizacion.domain.perfilamiento.modelo_riesgo import ModeloRiesgo
from cotizacion.domain.perfilamiento.modelos import (
    DecisionConsentimiento,
    PlanFuentes,
    SolicitudFuente,
    VerificacionConsentimiento,
)


def plan_fuentes(modelo: ModeloRiesgo, verificacion: VerificacionConsentimiento) -> PlanFuentes:
    """Solo las fuentes autorizadas; ninguna si falta un obligatorio no autorizado (minimización)."""
    permitidas = (
        frozenset()
        if verificacion.decision is DecisionConsentimiento.RECHAZADO
        else verificacion.alcances_permitidos
    )
    no_autorizadas = tuple(f for f in modelo.fuentes if f not in permitidas)
    autorizadas = tuple(f for f in modelo.fuentes if f in permitidas)
    falta_obligatorio = any(
        modelo.fuente_de_campo(campo) in no_autorizadas for campo in modelo.obligatorios
    )
    if falta_obligatorio:
        return PlanFuentes(
            a_consultar=(), no_autorizadas=no_autorizadas, no_consultadas=autorizadas
        )
    return PlanFuentes(
        a_consultar=tuple(SolicitudFuente(f, modelo.fuentes[f]) for f in autorizadas),
        no_autorizadas=no_autorizadas,
        no_consultadas=(),
    )
