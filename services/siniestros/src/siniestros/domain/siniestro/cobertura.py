"""Verificación de cobertura (HU-W19, DS3-05). Función pura sobre la copia de la póliza guardada al
reportar. Devuelve siempre seis verificaciones, en el orden de `CodigoVerificacion`.

El `detalle` son parámetros para el texto del portal (cadenas, nunca texto libre). Un dato que falta
en la copia vuelve la verificación `NO_EVALUABLE`: no se asume un valor.

Las fechas de vigencia son días de calendario del negocio: el instante del evento se lleva a la
hora de Colombia (UTC−5, sin horario de verano) antes de compararlo.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta, timezone

from siniestros.domain.siniestro.modelos import (
    CodigoVerificacion,
    CopiaPoliza,
    EstadoSoporte,
    EventoReclamado,
    Evidencia,
    ResultadoVerificacion,
    TipoSoporte,
)

COLOMBIA = timezone(timedelta(hours=-5), "COT")
_MINUTO = timedelta(minutes=1)


@dataclass(frozen=True)
class Verificacion:
    codigo: CodigoVerificacion
    resultado: ResultadoVerificacion
    detalle: Mapping[str, str]


@dataclass(frozen=True)
class SoportePendiente:
    tipo: TipoSoporte
    estado: EstadoSoporte


def _resultado(cumple: bool) -> ResultadoVerificacion:
    return ResultadoVerificacion.CUMPLE if cumple else ResultadoVerificacion.NO_CUMPLE


def _soportes_presentes(
    requeridos: Sequence[TipoSoporte], evidencias: Sequence[Evidencia]
) -> frozenset[TipoSoporte]:
    aportados = {e.soporte_tipo for e in evidencias if e.soporte_tipo is not None}
    return frozenset(tipo for tipo in requeridos if tipo in aportados)


def _sin_repetir(soportes: Collection[TipoSoporte]) -> tuple[TipoSoporte, ...]:
    return tuple(dict.fromkeys(soportes))


def verificar_cobertura(
    copia: CopiaPoliza,
    evento: EventoReclamado,
    evidencias: Sequence[Evidencia],
    soportes_requeridos: Collection[TipoSoporte],
) -> tuple[Verificacion, ...]:
    """Las seis verificaciones de cobertura del caso (DS3-05)."""
    dia_evento = evento.fecha_evento.astimezone(COLOMBIA).date()
    vigencia = copia.vigencia

    if vigencia is None:
        vigente = Verificacion(
            CodigoVerificacion.VIGENCIA,
            ResultadoVerificacion.NO_EVALUABLE,
            {"fechaEvento": dia_evento.isoformat()},
        )
    else:
        vigente = Verificacion(
            CodigoVerificacion.VIGENCIA,
            _resultado(vigencia.desde <= dia_evento <= vigencia.hasta),
            {
                "fechaEvento": dia_evento.isoformat(),
                "desde": vigencia.desde.isoformat(),
                "hasta": vigencia.hasta.isoformat(),
            },
        )

    if copia.amparos:
        amparo = Verificacion(
            CodigoVerificacion.AMPARO,
            _resultado(evento.causa in {a.codigo for a in copia.amparos}),
            {"amparo": evento.causa},
        )
    else:
        amparo = Verificacion(
            CodigoVerificacion.AMPARO,
            ResultadoVerificacion.NO_EVALUABLE,
            {"amparo": evento.causa},
        )

    if vigencia is None or copia.carencia_dias is None:
        carencia = Verificacion(CodigoVerificacion.CARENCIA, ResultadoVerificacion.NO_EVALUABLE, {})
    else:
        dias = (dia_evento - vigencia.desde).days
        carencia = Verificacion(
            CodigoVerificacion.CARENCIA,
            _resultado(dias >= copia.carencia_dias),
            {"diasDesdeInicio": str(dias), "carenciaDias": str(copia.carencia_dias)},
        )

    if copia.plazo_aviso_dias is None:
        plazo = Verificacion(CodigoVerificacion.PLAZO_AVISO, ResultadoVerificacion.NO_EVALUABLE, {})
    else:
        transcurrido = evento.reportado_en - evento.fecha_evento
        plazo = Verificacion(
            CodigoVerificacion.PLAZO_AVISO,
            _resultado(transcurrido <= timedelta(days=copia.plazo_aviso_dias)),
            {
                "minutosDesdeEvento": str(transcurrido // _MINUTO),
                "plazoAvisoDias": str(copia.plazo_aviso_dias),
            },
        )

    if copia.primas_al_dia is None:
        primas = Verificacion(CodigoVerificacion.PRIMAS, ResultadoVerificacion.NO_EVALUABLE, {})
    else:
        primas = Verificacion(
            CodigoVerificacion.PRIMAS,
            _resultado(copia.primas_al_dia),
            {"primasAlDia": "true" if copia.primas_al_dia else "false"},
        )

    requeridos = _sin_repetir(soportes_requeridos)
    presentes = _soportes_presentes(requeridos, evidencias)
    if not requeridos:
        soportes = Verificacion(
            CodigoVerificacion.SOPORTES,
            ResultadoVerificacion.NO_EVALUABLE,
            {"presentes": "0", "requeridos": "0"},
        )
    else:
        if len(presentes) == len(requeridos):
            resultado = ResultadoVerificacion.CUMPLE
        elif presentes:
            resultado = ResultadoVerificacion.PARCIAL
        else:
            resultado = ResultadoVerificacion.NO_CUMPLE
        soportes = Verificacion(
            CodigoVerificacion.SOPORTES,
            resultado,
            {"presentes": str(len(presentes)), "requeridos": str(len(requeridos))},
        )

    return (vigente, amparo, carencia, plazo, primas, soportes)


def soportes_pendientes(
    soportes_requeridos: Collection[TipoSoporte], evidencias: Sequence[Evidencia]
) -> tuple[SoportePendiente, ...]:
    """Los soportes requeridos que aún no tienen archivo, en el orden de la configuración. Salen
    `FALTA`; `SOLICITADO` lo agrega W20 cuando el analista los pide."""
    requeridos = _sin_repetir(soportes_requeridos)
    presentes = _soportes_presentes(requeridos, evidencias)
    return tuple(
        SoportePendiente(tipo, EstadoSoporte.FALTA) for tipo in requeridos if tipo not in presentes
    )
