"""Regla de evaluación paramétrica (HU-W21, DS3-21): función pura con valores límite.

Una cobertura **activa** si se cumplen las cinco condiciones: (1) está `ACTIVA` en la copia local,
(2) mismo `tipoEvento`, (3) misma `zona`, (4) `observadoEn` dentro de `[vigenteDesde, vigenteHasta)`
y (5) `valorObservado ≥ umbral` (justo en el umbral activa; una unidad abajo, no).

Si no activa, la razón es la primera que falla: `POLIZA_NO_ACTIVA`, `FUERA_DE_VENTANA`,
`FUERA_DE_ZONA`, `BAJO_UMBRAL`. A nivel de evento, sin candidatas: `SIN_POLIZAS_EN_ZONA`.

El tipo de evento no es una razón: una cobertura de otro tipo no es candidata y no se evalúa ni se
registra. Las candidatas salen del índice `(tipo_evento, zona)` sobre las `ACTIVA` (DS3-19); por eso
`POLIZA_NO_ACTIVA` y `FUERA_DE_ZONA` no aparecen por póliza en el recorrido completo (R-151), pero
siguen aquí con su precedencia: los usa la verificación de H11 y cualquier búsqueda más amplia.
"""

from collections.abc import Iterable

from pagos_parametricos.domain.parametrico.modelos import (
    Cobertura,
    DecisionEvento,
    DecisionPoliza,
    EstadoCobertura,
    Evaluacion,
    EvaluacionEvento,
    EventoObservado,
    Razon,
)


def _evaluar_cobertura(evento: EventoObservado, cobertura: Cobertura) -> Evaluacion:
    regla = cobertura.regla
    if cobertura.estado is not EstadoCobertura.ACTIVA or regla is None:
        return _no_activa(cobertura, Razon.POLIZA_NO_ACTIVA)
    if not regla.vigente_desde <= evento.observado_en < regla.vigente_hasta:
        return _no_activa(cobertura, Razon.FUERA_DE_VENTANA)
    if regla.zona != evento.zona:
        return _no_activa(cobertura, Razon.FUERA_DE_ZONA)
    if evento.valor_observado < regla.umbral:
        return _no_activa(cobertura, Razon.BAJO_UMBRAL)
    return Evaluacion(
        poliza_id=cobertura.poliza_id,
        decision=DecisionPoliza.ACTIVA,
        razon=None,
        version_regla=regla.version_regla,
        valor_pago=regla.valor_pago,
        moneda=regla.moneda,
    )


def _no_activa(cobertura: Cobertura, razon: Razon) -> Evaluacion:
    regla = cobertura.regla
    return Evaluacion(
        poliza_id=cobertura.poliza_id,
        decision=DecisionPoliza.NO_ACTIVA,
        razon=razon,
        version_regla=regla.version_regla if regla else None,
    )


def evaluar(evento: EventoObservado, candidatas: Iterable[Cobertura]) -> EvaluacionEvento:
    """Evalúa el evento contra las coberturas candidatas, ordenadas por póliza (determinista)."""
    comparables = [
        c for c in candidatas if c.regla is not None and c.regla.tipo_evento == evento.tipo_evento
    ]
    evaluaciones = tuple(
        _evaluar_cobertura(evento, c) for c in sorted(comparables, key=lambda c: str(c.poliza_id))
    )
    if not evaluaciones:
        return EvaluacionEvento(DecisionEvento.SIN_POLIZAS, Razon.SIN_POLIZAS_EN_ZONA, ())
    activa = any(e.decision is DecisionPoliza.ACTIVA for e in evaluaciones)
    decision = DecisionEvento.ACTIVA if activa else DecisionEvento.NO_ACTIVA
    return EvaluacionEvento(decision, None, evaluaciones)
