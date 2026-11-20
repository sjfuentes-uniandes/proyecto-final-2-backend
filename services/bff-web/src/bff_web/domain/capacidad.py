"""Capacidad de un servicio ECS para Operación › Tablero (HU-W08).

La serie de cotizaciones trae un punto por minuto de la ventana, con 0 donde CloudWatch no tiene
dato. El último escalado es la actividad de Application Auto Scaling más reciente que empezó
dentro de la ventana; si sigue en curso, no tiene fin ni duración.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class Punto:
    instante: datetime
    valor: int


@dataclass(frozen=True)
class Escalado:
    inicio: datetime
    fin: datetime | None
    duracion_s: int | None
    descripcion: str
    estado: str


@dataclass(frozen=True)
class PanelCapacidad:
    servicio: str
    ventana_min: int
    cotizaciones_por_minuto: list[Punto]
    tareas_activas: int | None  # None: sin puntos de CPUUtilization en la ventana
    minimo: int
    maximo: int
    ultimo_escalado: Escalado | None


def al_minuto(instante: datetime) -> datetime:
    return instante.astimezone(UTC).replace(second=0, microsecond=0)


def serie_por_minuto(
    valores: Mapping[datetime, float], desde: datetime, minutos: int
) -> list[Punto]:
    """Exactamente `minutos` puntos desde `desde`, en orden ascendente; 0 si no hay dato."""
    por_minuto = {al_minuto(instante): valor for instante, valor in valores.items()}
    instantes = (desde + timedelta(minutes=n) for n in range(minutos))
    return [Punto(t, round(por_minuto.get(t, 0))) for t in instantes]


def ultimo_valor(valores: Mapping[datetime, float]) -> int | None:
    """El valor del instante más reciente; None si no hay datos (no se sabe, no es 0)."""
    if not valores:
        return None
    return round(valores[max(valores)])


def ultimo_escalado(actividades: Iterable[Mapping[str, Any]], desde: datetime) -> Escalado | None:
    dentro = [a for a in actividades if a["StartTime"] >= desde]
    if not dentro:
        return None
    reciente = max(dentro, key=lambda a: a["StartTime"])
    inicio: datetime = reciente["StartTime"]
    fin: datetime | None = reciente.get("EndTime")
    return Escalado(
        inicio=inicio,
        fin=fin,
        duracion_s=round((fin - inicio).total_seconds()) if fin is not None else None,
        descripcion=str(reciente.get("Description", "")),
        estado=str(reciente.get("StatusCode", "")),
    )
