"""Motor de suscripción automática (HU-W13, DS2-06). Puro y determinista: sin E/S ni reloj.

Precedencia:
1. Si alguna regla EXCLUYENTE no se cumple → RECHAZADA; la regla determinante es la primera por
   `orden`.
2. Si no, si alguna regla REVISION no se cumple o no es evaluable, o una EXCLUYENTE no es evaluable
   → REVISION_REQUERIDA (el motor nunca rechaza ni aprueba sin el dato).
3. Si todas se cumplen → APROBADA.

Un insumo ausente (sin perfil, nivel nulo por INFORMACION_INSUFICIENTE, listas no consultadas)
deja la regla NO_EVALUABLE. Los motivos para el cliente son los de las reglas que deciden, en
orden y sin repetir; una regla RESERVADA solo aporta su motivo (NO_ELEGIBLE) en un rechazo (DS2-07).
"""

from collections.abc import Iterable, Sequence
from decimal import Decimal

from polizas.domain.suscripcion.insumos import texto
from polizas.domain.suscripcion.modelos import (
    Evaluacion,
    InsumosSuscripcion,
    Regla,
    ReglaEvaluada,
    ResultadoDecision,
    ResultadoRegla,
    TipoRegla,
    Visibilidad,
)

ValorCaso = Decimal | str | None


def valor_del_caso(insumos: InsumosSuscripcion, campo: str) -> ValorCaso:
    """El insumo que evalúa la regla; None si no está disponible."""
    perfil = insumos.perfil
    match campo:
        case "edad":
            return Decimal(insumos.edad)
        case "suma_asegurada":
            return insumos.suma_asegurada
        case "plazo_credito_meses":
            return Decimal(insumos.plazo_credito_meses)
        case "nivel_riesgo":
            return perfil.nivel_riesgo.value if perfil and perfil.nivel_riesgo else None
        case "confianza_perfil":
            return perfil.confianza if perfil else None
        case "coincidencias_listas_restrictivas":
            coincidencias = insumos.coincidencias_listas_restrictivas
            return None if coincidencias is None else Decimal(coincidencias)
    # Un campo fuera del conjunto cerrado no se puede evaluar (catalogo lo impide al publicar).
    return None


def cumple(regla: Regla, valor: Decimal | str) -> bool:
    """La condición de la regla sobre un valor disponible (decimal o categoría)."""
    if isinstance(valor, Decimal):
        return _cumple_numerica(regla, valor)
    return _cumple_categorica(regla, valor)


def _cumple_numerica(regla: Regla, valor: Decimal) -> bool:
    match regla.operador, regla.valor:
        case ">=", str(referencia):
            return valor >= Decimal(referencia)
        case "<=", str(referencia):
            return valor <= Decimal(referencia)
        case "==", str(referencia):
            return valor == Decimal(referencia)
        case "!=", str(referencia):
            return valor != Decimal(referencia)
        case "entre", (str(minimo), str(maximo)):
            return Decimal(minimo) <= valor <= Decimal(maximo)
        case "en", tuple(referencias):
            return valor in {Decimal(r) for r in referencias}
    raise ValueError(f"regla fuera del contrato: {regla.codigo}")


def _cumple_categorica(regla: Regla, valor: str) -> bool:
    match regla.operador, regla.valor:
        case "==", str(referencia):
            return valor == referencia
        case "!=", str(referencia):
            return valor != referencia
        case "en", tuple(referencias):
            return valor in referencias
    raise ValueError(f"regla fuera del contrato: {regla.codigo}")


def evaluar_regla(regla: Regla, insumos: InsumosSuscripcion) -> ReglaEvaluada:
    valor = valor_del_caso(insumos, regla.campo)
    if valor is None:
        return ReglaEvaluada(regla, None, ResultadoRegla.NO_EVALUABLE)
    resultado = ResultadoRegla.CUMPLE if cumple(regla, valor) else ResultadoRegla.NO_CUMPLE
    caso = texto(valor) if isinstance(valor, Decimal) else valor
    return ReglaEvaluada(regla, caso, resultado)


def _motivos(evaluadas: Iterable[ReglaEvaluada], rechazo: bool) -> tuple[str, ...]:
    motivos = (
        r.regla.motivo_cliente
        for r in evaluadas
        if rechazo or r.regla.visibilidad is not Visibilidad.RESERVADA
    )
    return tuple(dict.fromkeys(motivos))


def evaluar(insumos: InsumosSuscripcion, reglas: Sequence[Regla]) -> Evaluacion:
    evaluadas = tuple(evaluar_regla(r, insumos) for r in sorted(reglas, key=lambda r: r.orden))
    excluidas = [
        r
        for r in evaluadas
        if r.regla.tipo is TipoRegla.EXCLUYENTE and r.resultado is ResultadoRegla.NO_CUMPLE
    ]
    if excluidas:
        return Evaluacion(
            ResultadoDecision.RECHAZADA,
            evaluadas,
            excluidas[0].regla.codigo,
            _motivos(excluidas, rechazo=True),
        )
    pendientes = [r for r in evaluadas if r.resultado is not ResultadoRegla.CUMPLE]
    if pendientes:
        return Evaluacion(
            ResultadoDecision.REVISION_REQUERIDA,
            evaluadas,
            None,
            _motivos(pendientes, rechazo=False),
        )
    return Evaluacion(ResultadoDecision.APROBADA, evaluadas, None, ())
