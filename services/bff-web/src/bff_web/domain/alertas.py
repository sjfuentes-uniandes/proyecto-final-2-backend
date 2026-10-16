"""Alertas abiertas, umbrales y resumen del mes (HU-W28). Catálogo en ALERTAS_CATALOGO (infra/apps/alarms.tf)."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

SEVERIDADES = ("critica", "advertencia", "informativa")
ESTADO_ALARMA = "ALARM"


@dataclass(frozen=True)
class EntradaCatalogo:
    severidad: str
    titulo: str
    unidad: str
    enlace: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Umbral:
    metrica: str
    unidad: str
    meta: float
    advertencia: float
    critica: float


@dataclass(frozen=True)
class AlertaAbierta:
    alarma: str
    severidad: str
    titulo: str
    valor_actual: float | None
    umbral: float
    unidad: str
    abierta_desde: datetime
    enlace: dict[str, Any]


@dataclass(frozen=True)
class Transicion:
    alarma: str
    momento: datetime
    anterior: str
    nuevo: str


@dataclass(frozen=True)
class ResumenMes:
    desde: datetime
    generadas: int
    criticas: int
    falsos_positivos: int
    tiempo_medio_atencion_min: int | None
    metas_incumplidas: int


@dataclass(frozen=True)
class PanelAlertas:
    abiertas: list[AlertaAbierta]
    umbrales: list[Umbral]
    resumen: ResumenMes


def leer_catalogo(crudo: Mapping[str, Mapping[str, Any]]) -> dict[str, EntradaCatalogo]:
    return {
        nombre: EntradaCatalogo(e["severidad"], e["titulo"], e["unidad"], e.get("enlace") or {})
        for nombre, e in crudo.items()
        if e.get("severidad") in SEVERIDADES
    }


def leer_umbrales(crudo: Iterable[Mapping[str, Any]]) -> list[Umbral]:
    return [Umbral(u["metrica"], u["unidad"], u["meta"], u["advertencia"], u["critica"]) for u in crudo]


def valor_observado(reason_data: str | None) -> float | None:
    """Último dato evaluado de StateReasonData (métricas simples, metric math o Metrics Insights)."""
    try:
        datos = json.loads(reason_data or "{}")
    except ValueError:
        return None
    recientes = datos.get("recentDatapoints") or []
    if recientes:
        return float(recientes[-1])
    evaluados = [d.get("value") for d in datos.get("evaluatedDatapoints") or [] if d.get("value") is not None]
    return float(evaluados[0]) if evaluados else None


def alerta_abierta(alarma: Mapping[str, Any], prefijo: str, catalogo: Mapping[str, EntradaCatalogo]) -> AlertaAbierta | None:
    nombre = str(alarma["AlarmName"])
    entrada = catalogo.get(nombre.removeprefix(prefijo)) if nombre.startswith(prefijo) else None
    if entrada is None or alarma.get("StateValue") != ESTADO_ALARMA:
        return None
    return AlertaAbierta(
        alarma=nombre,
        severidad=entrada.severidad,
        titulo=entrada.titulo,
        valor_actual=valor_observado(alarma.get("StateReasonData")),
        umbral=float(alarma["Threshold"]),
        unidad=entrada.unidad,
        abierta_desde=alarma.get("StateTransitionedTimestamp") or alarma["StateUpdatedTimestamp"],
        enlace=dict(entrada.enlace),
    )


def ordenar(abiertas: Iterable[AlertaAbierta]) -> list[AlertaAbierta]:
    """Primero las más severas; dentro de cada severidad, las más recientes."""
    por_fecha = sorted(abiertas, key=lambda a: a.abierta_desde, reverse=True)
    return sorted(por_fecha, key=lambda a: SEVERIDADES.index(a.severidad))


def resumir(
    transiciones: Iterable[Transicion],
    catalogo: Mapping[str, EntradaCatalogo],
    prefijo: str,
    desde: datetime,
    ahora: datetime,
    falso_positivo_min: float,
    atencion_min: float,
) -> ResumenMes:
    """Episodio: de la entrada a ALARM a la siguiente salida. Criterios en docs/hu-w28-alertas.md."""
    inicios: dict[str, datetime] = {}
    episodios: list[tuple[str, timedelta, bool]] = []
    for t in sorted(transiciones, key=lambda t: t.momento):
        nombre = t.alarma.removeprefix(prefijo)
        if not t.alarma.startswith(prefijo) or nombre not in catalogo:
            continue
        if t.nuevo == ESTADO_ALARMA and t.anterior != ESTADO_ALARMA:
            inicios[nombre] = t.momento
        elif t.anterior == ESTADO_ALARMA and nombre in inicios:
            episodios.append((nombre, t.momento - inicios.pop(nombre), True))
    episodios.extend((nombre, ahora - inicio, False) for nombre, inicio in inicios.items())

    cerrados = [duracion for _, duracion, cerrado in episodios if cerrado]
    criticos = [duracion for nombre, duracion, _ in episodios if catalogo[nombre].severidad == "critica"]
    medio = sum(cerrados, timedelta()) / len(cerrados) if cerrados else None
    return ResumenMes(
        desde=desde,
        generadas=len(episodios),
        criticas=len(criticos),
        falsos_positivos=sum(1 for d in cerrados if d < timedelta(minutes=falso_positivo_min)),
        tiempo_medio_atencion_min=round(medio.total_seconds() / 60) if medio is not None else None,
        metas_incumplidas=sum(1 for d in criticos if d > timedelta(minutes=atencion_min)),
    )
