"""Cambios de estado del siniestro (HU-W19 y HU-W20, DS3-02). Puro: el instante llega como
argumento y el dominio no lee ningún reloj.

W19 ejecuta `REPORTADO → EN_EVALUACION` (tomar el siguiente de la cola; **abrir un caso no lo
asigna**). W20 agrega la decisión del analista (aprobar, rechazar, solicitar soporte), el
vencimiento del plazo del soporte y sus validaciones. Los paramétricos (W22) nacen `APROBADO`: no son
una transición.

La tabla `TRANSICIONES` es la única fuente de qué se puede hacer desde cada estado: cada par
(estado × acción) que no está en ella es inválido (409 `TRANSICION_NO_PERMITIDA`).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID

from siniestros.domain.siniestro.errores import (
    CasoAsignadoAOtro,
    CausalInvalida,
    DecisionInvalida,
    JustificacionRequerida,
    SiniestroInvalido,
    TransicionInvalida,
    ValorFueraDeRango,
)
from siniestros.domain.siniestro.modelos import (
    AccionSiniestro,
    CausalRechazo,
    EstadoSiniestro,
    Siniestro,
    con_zona,
)

# Hora de Colombia (UTC−5, sin horario de verano): la misma regla que la cobertura (R-20).
COLOMBIA: Final = timezone(timedelta(hours=-5), "COT")


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


# --- Tabla de transiciones (DS3-02) --------------------------------------------------------------

_E = EstadoSiniestro
_A = AccionSiniestro

TRANSICIONES: Final[Mapping[tuple[EstadoSiniestro, AccionSiniestro], EstadoSiniestro]] = {
    (_E.REPORTADO, _A.ASIGNAR): _E.EN_EVALUACION,
    (_E.EN_EVALUACION, _A.APROBAR): _E.APROBADO,
    (_E.EN_EVALUACION, _A.RECHAZAR): _E.RECHAZADO,
    (_E.EN_EVALUACION, _A.SOLICITAR_SOPORTE): _E.FALTA_SOPORTE,
    (_E.FALTA_SOPORTE, _A.VENCER_PLAZO_SOPORTE): _E.EN_EVALUACION,
}

# A dónde llevaría cada acción: es lo que informa `TransicionInvalida` cuando el estado no la admite.
DESTINO_NOMINAL: Final[Mapping[AccionSiniestro, EstadoSiniestro]] = {
    _A.ASIGNAR: _E.EN_EVALUACION,
    _A.APROBAR: _E.APROBADO,
    _A.RECHAZAR: _E.RECHAZADO,
    _A.SOLICITAR_SOPORTE: _E.FALTA_SOPORTE,
    _A.VENCER_PLAZO_SOPORTE: _E.EN_EVALUACION,
}


def estado_destino(estado: EstadoSiniestro, accion: AccionSiniestro) -> EstadoSiniestro:
    """El estado al que lleva `accion` desde `estado`; si no lo admite, `TransicionInvalida`."""
    destino = TRANSICIONES.get((estado, accion))
    if destino is None:
        raise TransicionInvalida(estado, DESTINO_NOMINAL[accion])
    return destino


def verificar_decision(
    siniestro: Siniestro, accion: AccionSiniestro, analista_id: UUID
) -> EstadoSiniestro:
    """Comprobaciones 5 y 6 del orden de la decisión (guía W20, §3): primero la transición y luego
    el analista asignado, así un caso ya decidido siempre es `TransicionInvalida`, sin revelar a
    quién pertenecía."""
    destino = estado_destino(siniestro.estado, accion)
    if siniestro.analista_id != analista_id:
        raise CasoAsignadoAOtro
    return destino


# --- Validaciones de la decisión (DS3-12) --------------------------------------------------------

_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")
_CENTAVOS = Decimal("0.01")
_MAX_ENTEROS = 16  # numeric(18,2)
DIAS_PLAZO_SOPORTE: Final = (5, 10, 15)


def validar_justificacion(texto: str) -> str:
    """Obligatoria en las tres decisiones: vacía o solo espacios es `JustificacionRequerida`."""
    limpia = texto.strip()
    if not limpia:
        raise JustificacionRequerida
    return limpia


def validar_causal(texto: str) -> CausalRechazo:
    """Una causal de la lista cerrada; cualquier otra cosa (o nada) es `CausalInvalida`."""
    try:
        return CausalRechazo(texto.strip())
    except ValueError as error:
        raise CausalInvalida from error


def validar_texto_cliente(campo: str, texto: str) -> str:
    """`explicacionCliente` y `mensajeCliente`: no pueden venir vacíos."""
    limpio = texto.strip()
    if not limpio:
        raise DecisionInvalida([campo])
    return limpio


def validar_documentos[T](documentos: Sequence[T]) -> tuple[T, ...]:
    """Al menos uno y sin repetidos, en el orden en que se pidieron."""
    unicos = tuple(dict.fromkeys(documentos))
    if not unicos or len(unicos) != len(documentos):
        raise DecisionInvalida(["documentos"])
    return unicos


def validar_plazo(dias_habiles: int) -> int:
    if dias_habiles not in DIAS_PLAZO_SOPORTE:
        raise DecisionInvalida(["plazoDiasHabiles"])
    return dias_habiles


@dataclass(frozen=True)
class Liquidacion:
    """Lo que el analista aprueba pagar (H03). Montos con a lo sumo dos decimales."""

    valor_reclamado: Decimal
    deducible: Decimal
    valor_indemnizar: Decimal
    moneda: str


def _monto(texto: str) -> Decimal | None:
    """El monto si es un decimal con a lo sumo dos decimales significativos y 16 enteros."""
    if _DECIMAL.fullmatch(texto) is None:
        return None
    try:
        valor = Decimal(texto)
    except InvalidOperation:  # pragma: no cover - el patrón ya lo impide
        return None
    if valor != valor.quantize(_CENTAVOS) or abs(valor) >= Decimal(10) ** _MAX_ENTEROS:
        return None
    return valor.quantize(_CENTAVOS)


def validar_liquidacion(
    *,
    valor_reclamado: str,
    deducible: str,
    valor_indemnizar: str,
    moneda: str,
    suma_asegurada: str,
    moneda_poliza: str,
    valor_reclamado_registrado: Decimal | None = None,
    moneda_registrada: str | None = None,
) -> Liquidacion:
    """Reglas de rango de la liquidación (R-67). Junta **todos** los campos que fallan en un solo
    `ValorFueraDeRango`: `0 < valorIndemnizar ≤ valorReclamado`, `0 ≤ deducible ≤ valorReclamado`,
    `valorIndemnizar ≤ suma asegurada`, la moneda de la póliza y, si el siniestro ya trae el valor
    reclamado, que coincida. Sin dato registrado se acepta el de la solicitud."""
    reclamado = _monto(valor_reclamado)
    descuento = _monto(deducible)
    indemnizar = _monto(valor_indemnizar)
    asegurada = _monto(suma_asegurada)
    fallan: list[str] = []

    distinto_del_registrado = (
        reclamado is not None
        and valor_reclamado_registrado is not None
        and reclamado != valor_reclamado_registrado
    )
    if reclamado is None or reclamado <= 0 or distinto_del_registrado:
        fallan.append("liquidacion.valorReclamado")
    if descuento is None or descuento < 0 or (reclamado is not None and descuento > reclamado):
        fallan.append("liquidacion.deducible")
    if (
        indemnizar is None
        or indemnizar <= 0
        or (reclamado is not None and indemnizar > reclamado)
        or (asegurada is not None and indemnizar > asegurada)
    ):
        fallan.append("liquidacion.valorIndemnizar")
    esperadas = {moneda_poliza} | ({moneda_registrada} if moneda_registrada else set())
    if esperadas != {moneda}:
        fallan.append("liquidacion.moneda")
    if fallan:
        raise ValorFueraDeRango(fallan)
    assert reclamado is not None and descuento is not None and indemnizar is not None
    return Liquidacion(reclamado, descuento, indemnizar, moneda)


# --- Días hábiles y referencia de pago -----------------------------------------------------------

DIAS_HABILES_POR_SEMANA: Final = 5  # lunes (0) a viernes (4)


def sumar_dias_habiles(desde: datetime, dias: int) -> datetime:
    """`desde` + `dias` días hábiles (lunes a viernes, **sin festivos**, DS3-12). La fecha es la de
    Colombia; se cuenta desde el día **siguiente** y se conserva la hora local (R-68)."""
    if not con_zona(desde):
        raise SiniestroInvalido("desde: debe traer zona horaria")
    if dias < 1:
        raise SiniestroInvalido("dias: debe ser al menos 1")
    local = desde.astimezone(COLOMBIA)
    pendientes = dias
    while pendientes:
        local += timedelta(days=1)
        if local.weekday() < DIAS_HABILES_POR_SEMANA:
            pendientes -= 1
    return local.astimezone(desde.tzinfo)


def referencia_pago(codigo: str) -> str:
    """`IND-<codigo>-01` (DS3-11): la clave idempotente del pago único de HU-W23."""
    return f"IND-{codigo}-01"


# --- Transiciones de la decisión -----------------------------------------------------------------


def _con_hora(ahora: datetime) -> None:
    if not con_zona(ahora):
        raise SiniestroInvalido("ahora: debe traer zona horaria")


def aprobar(siniestro: Siniestro, liquidacion: Liquidacion, ahora: datetime) -> Siniestro:
    """`EN_EVALUACION → APROBADO`: guarda la liquidación y detiene el reloj en `ahora`."""
    destino = estado_destino(siniestro.estado, AccionSiniestro.APROBAR)
    _con_hora(ahora)
    return replace(
        siniestro,
        estado=destino,
        decidido_en=ahora,
        deducible=liquidacion.deducible,
        valor_indemnizar=liquidacion.valor_indemnizar,
        version=siniestro.version + 1,
    )


def rechazar(siniestro: Siniestro, ahora: datetime) -> Siniestro:
    """`EN_EVALUACION → RECHAZADO`: no habilita pago y detiene el reloj en `ahora`."""
    destino = estado_destino(siniestro.estado, AccionSiniestro.RECHAZAR)
    _con_hora(ahora)
    return replace(siniestro, estado=destino, decidido_en=ahora, version=siniestro.version + 1)


def solicitar_soporte(siniestro: Siniestro, dias_habiles: int, ahora: datetime) -> Siniestro:
    """`EN_EVALUACION → FALTA_SOPORTE`: pausa el reloj y fija el fin del plazo (en días hábiles)."""
    destino = estado_destino(siniestro.estado, AccionSiniestro.SOLICITAR_SOPORTE)
    _con_hora(ahora)
    validar_plazo(dias_habiles)
    return replace(
        siniestro,
        estado=destino,
        pausado_desde=ahora,
        plazo_soporte_vence_en=sumar_dias_habiles(ahora, dias_habiles),
        version=siniestro.version + 1,
    )


def plazo_soporte_vencido(siniestro: Siniestro, ahora: datetime) -> bool:
    """El plazo venció cuando `ahora` es posterior a `plazo_soporte_vence_en` (DS3-13)."""
    _con_hora(ahora)
    return (
        siniestro.estado is EstadoSiniestro.FALTA_SOPORTE
        and siniestro.plazo_soporte_vence_en is not None
        and siniestro.plazo_soporte_vence_en < ahora
    )


def vencer_plazo_soporte(siniestro: Siniestro, ahora: datetime) -> Siniestro:
    """`FALTA_SOPORTE → EN_EVALUACION` con el **mismo** analista (DS3-13, v1.3): reanuda el reloj
    (suma la pausa a `pausa_acumulada_s`) y limpia la pausa y el plazo."""
    destino = estado_destino(siniestro.estado, AccionSiniestro.VENCER_PLAZO_SOPORTE)
    if not plazo_soporte_vencido(siniestro, ahora):
        raise SiniestroInvalido("ahora: el plazo del soporte no ha vencido")
    assert siniestro.pausado_desde is not None  # lo garantiza la invariante de FALTA_SOPORTE
    pausa = max(0, int((ahora - siniestro.pausado_desde).total_seconds()))
    return replace(
        siniestro,
        estado=destino,
        pausado_desde=None,
        plazo_soporte_vence_en=None,
        pausa_acumulada_s=siniestro.pausa_acumulada_s + pausa,
        version=siniestro.version + 1,
    )
