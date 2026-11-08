"""Coherencia de la evidencia (HU-W19, DS3-06). Función pura: la integridad de cada objeto la
calcula el adaptador de S3 (`HeadObject` con checksum) y llega como argumento.

| Dato                | Cálculo                                                                          |
| ------------------- | -------------------------------------------------------------------------------- |
| `diferencia_min`    | `primera_captura − fecha_declarada`, en minutos (hacia abajo)                    |
| `ubicacion_coincide`| distancia a la primera evidencia con coordenadas ≤ `distancia_max_m`; `None` si falta alguna |
| `archivos_alterados`| evidencias con integridad `ALTERADA`                                             |
| `resultado`         | `INCONSISTENTE` si `diferencia < 0`, la ubicación no coincide o hay alterados;   |
|                     | `NO_EVALUABLE` si ninguna evidencia trae fecha de captura; `COHERENTE` si no    |

Un objeto `NO_DISPONIBLE` o `NO_VERIFICABLE` no vuelve inconsistente el caso (AC4): no se sabe que
esté alterado.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from siniestros.domain.siniestro.modelos import (
    Evidencia,
    IntegridadEvidencia,
    ResultadoCoherencia,
    Ubicacion,
)

DISTANCIA_MAX_M = 1000
_RADIO_TIERRA_M = 6_371_008.8
_MINUTO = timedelta(minutes=1)
_SIN_FECHA = datetime.max.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Declarado:
    """Lo que declaró el cliente: cuándo y dónde ocurrió el evento."""

    fecha_evento: datetime
    ubicacion: Ubicacion


@dataclass(frozen=True)
class Coherencia:
    fecha_declarada: datetime
    primera_captura: datetime | None
    diferencia_min: int | None
    ubicacion_coincide: bool | None
    archivos_alterados: int
    resultado: ResultadoCoherencia


def distancia_m(a: Ubicacion, b: Ubicacion) -> float:
    """Distancia en metros entre dos puntos con coordenadas (haversine, esfera media)."""
    if a.lat is None or a.lon is None or b.lat is None or b.lon is None:
        raise ValueError("distancia_m: ambas ubicaciones necesitan coordenadas")
    fi1, fi2 = math.radians(a.lat), math.radians(b.lat)
    delta_fi = fi2 - fi1
    delta_lambda = math.radians(b.lon - a.lon)
    h = (
        math.sin(delta_fi / 2) ** 2
        + math.cos(fi1) * math.cos(fi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * _RADIO_TIERRA_M * math.asin(min(1.0, math.sqrt(h)))


def evaluar_coherencia(
    declarado: Declarado,
    evidencias: Sequence[Evidencia],
    integridades: Mapping[UUID, IntegridadEvidencia],
    *,
    distancia_max_m: int = DISTANCIA_MAX_M,
) -> Coherencia:
    """Compara lo declarado con lo capturado. Una evidencia sin integridad conocida cuenta como no
    alterada."""
    por_captura = sorted(evidencias, key=lambda e: e.capturada_en or _SIN_FECHA)
    capturas = [e.capturada_en for e in por_captura if e.capturada_en is not None]
    primera = capturas[0] if capturas else None
    diferencia = (primera - declarado.fecha_evento) // _MINUTO if primera is not None else None

    coincide: bool | None = None
    if declarado.ubicacion.tiene_coordenadas:
        capturada = next(
            (e.ubicacion for e in por_captura if e.ubicacion and e.ubicacion.tiene_coordenadas),
            None,
        )
        if capturada is not None:
            coincide = distancia_m(declarado.ubicacion, capturada) <= distancia_max_m

    alterados = sum(1 for e in evidencias if integridades.get(e.id) is IntegridadEvidencia.ALTERADA)

    if (diferencia is not None and diferencia < 0) or coincide is False or alterados > 0:
        resultado = ResultadoCoherencia.INCONSISTENTE
    elif primera is None:
        resultado = ResultadoCoherencia.NO_EVALUABLE
    else:
        resultado = ResultadoCoherencia.COHERENTE

    return Coherencia(
        fecha_declarada=declarado.fecha_evento,
        primera_captura=primera,
        diferencia_min=diferencia,
        ubicacion_coincide=coincide,
        archivos_alterados=alterados,
        resultado=resultado,
    )
