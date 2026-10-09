"""Traza de un recorrido para el back-office de operación (HU-W27, pantalla "Traza").

Convierte los segmentos de X-Ray (documentos JSON) en un desglose por servicio:
un tramo por cada servicio interno que atendió parte del recorrido y uno por
cada dependencia externa llamada (aliados, AWS). Solo muestra lo que la traza
contiene; no inventa datos de negocio.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

ESTADO_CORRECTO = "Correcto"
ESTADO_ERROR = "Error"
ESTADO_LIMITADO = "Limitado"

# Servicios por los que entra un recorrido (filtro "Recorrido" de la pantalla).
ENTRADAS = {"api-socios": "Socios", "bff-web": "Portal web", "bff-movil": "App móvil"}


@dataclass(frozen=True)
class FiltroTrazas:
    correlation_id: str | None = None
    entrada: str | None = None
    duracion_min_ms: int = 0
    horas: int = 6


@dataclass
class Tramo:
    id: str
    servicio: str
    operacion: str
    externo: bool
    inicio: float  # epoch en segundos
    fin: float
    estado: str
    padre: str | None = None  # id del tramo que lo invocó
    inicio_ms: int = 0
    duracion_ms: int = 0
    propio_ms: int = 0
    participacion: float = 0.0
    destacado: bool = False


@dataclass
class DetalleTraza:
    correlation_id: str | None
    trace_ids: list[str]
    inicio: datetime
    duracion_total_ms: int
    servicio_entrada: str
    resultado: str
    tramos: list[Tramo]
    interpretacion: str
    servicios_internos: int
    servicios_externos: int
    contexto: dict[str, str] = field(default_factory=dict)


def _estado(doc: Mapping[str, Any]) -> str:
    if doc.get("fault") or doc.get("error"):
        return ESTADO_ERROR
    if doc.get("throttle"):
        return ESTADO_LIMITADO
    return ESTADO_CORRECTO


def _subsegmentos(doc: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for sub in doc.get("subsegments", []) or []:
        yield sub
        yield from _subsegmentos(sub)


def _operacion(doc: Mapping[str, Any]) -> str:
    # Primera operación de negocio (telemetry.operacion) dentro del segmento.
    for sub in _subsegmentos(doc):
        operacion = (sub.get("annotations") or {}).get("operation")
        if operacion:
            return str(operacion)
    peticion = (doc.get("http") or {}).get("request") or {}
    ruta = urlsplit(str(peticion.get("url") or "")).path  # sin query string
    return f"{peticion.get('method', '')} {ruta}".strip() or str(doc.get("name", ""))


def _correlacion(doc: Mapping[str, Any]) -> str | None:
    for nodo in (doc, *_subsegmentos(doc)):
        valor = (nodo.get("annotations") or {}).get("correlation_id")
        if valor:
            return str(valor)
    return None


def _tramos(documentos: Iterable[Mapping[str, Any]]) -> list[Tramo]:
    segmentos = [doc for doc in documentos if doc.get("type") != "subsegment" and not doc.get("inferred")]
    internos = {str(doc.get("name")) for doc in segmentos}
    dueno: dict[str, str] = {}  # id de subsegmento -> id del segmento que lo contiene
    tramos: list[Tramo] = []
    for doc in segmentos:
        tramos.append(Tramo(
            id=str(doc["id"]), servicio=str(doc.get("name")), operacion=_operacion(doc), externo=False,
            inicio=float(doc["start_time"]), fin=float(doc.get("end_time") or doc["start_time"]),
            estado=_estado(doc), padre=doc.get("parent_id"),
        ))
        for sub in _subsegmentos(doc):
            dueno[str(sub.get("id"))] = str(doc["id"])
            # Llamadas a dependencias que no reportan su propio segmento (aliados, AWS).
            if sub.get("namespace") in ("remote", "aws") and str(sub.get("name")) not in internos:
                tramos.append(Tramo(
                    id=str(sub.get("id")), servicio=str(sub.get("name")), operacion="externo", externo=True,
                    inicio=float(sub["start_time"]), fin=float(sub.get("end_time") or sub["start_time"]),
                    estado=_estado(sub), padre=str(sub.get("id")),
                ))
    # El padre de un segmento es un subsegmento cliente del llamador: se traduce al tramo dueño.
    for tramo in tramos:
        if tramo.padre is not None:
            tramo.padre = dueno.get(tramo.padre)
    return sorted(tramos, key=lambda t: (t.inicio, t.externo))


def _calcular_tiempos(tramos: list[Tramo]) -> tuple[float, int]:
    inicio = min(t.inicio for t in tramos)
    fin = max(t.fin for t in tramos)
    total_ms = max(round((fin - inicio) * 1000), 1)
    for tramo in tramos:
        tramo.inicio_ms = round((tramo.inicio - inicio) * 1000)
        tramo.duracion_ms = round((tramo.fin - tramo.inicio) * 1000)
        tramo.participacion = round(min(tramo.duracion_ms / total_ms, 1.0), 4)
    for tramo in tramos:
        hijos = sum(h.duracion_ms for h in tramos if h.padre == tramo.id and h is not tramo)
        tramo.propio_ms = max(tramo.duracion_ms - hijos, 0)
    return inicio, total_ms


def formatear_ms(ms: int) -> str:
    return f"{ms:,}".replace(",", ".") + " ms"


def formatear_segundos(ms: int) -> str:
    return f"{ms / 1000:.2f}".replace(".", ",") + " s"


def _etiqueta(tramo: Tramo) -> str:
    return f"{tramo.servicio} · {tramo.operacion}"


def interpretar(tramos: list[Tramo], total_ms: int, umbral_ms: int) -> str:
    """Regla simple: los dos tramos con más tiempo propio, su peso y el umbral del recorrido."""
    if not tramos:
        return "La traza no tiene tramos registrados."
    costosos = sorted(tramos, key=lambda t: t.propio_ms, reverse=True)[:2]
    for tramo in costosos:
        tramo.destacado = True
    primero = costosos[0]
    if len(costosos) == 1:
        texto = f"El único paso registrado es {_etiqueta(primero)} con {formatear_ms(primero.propio_ms)}."
        candidatos = "este tramo es el candidato a optimizar"
    else:
        segundo = costosos[1]
        porcentaje = round(100 * (primero.propio_ms + segundo.propio_ms) / total_ms)
        texto = (
            f"El paso más costoso es {_etiqueta(primero)} con {formatear_ms(primero.propio_ms)}, "
            f"seguido por {_etiqueta(segundo)} con {formatear_ms(segundo.propio_ms)}. "
            f"Entre los dos consumen el {porcentaje} % del recorrido."
        )
        candidatos = "estos dos tramos son los candidatos a optimizar"
    if total_ms > umbral_ms:
        texto += (
            f" La duración total ({formatear_segundos(total_ms)}) supera el límite de "
            f"{formatear_segundos(umbral_ms)}: {candidatos}."
        )
    else:
        texto += f" La duración total está dentro del límite de {formatear_segundos(umbral_ms)}."
    fallidos = [t for t in tramos if t.estado == ESTADO_ERROR]
    if fallidos:
        texto += " Terminaron con error: " + ", ".join(_etiqueta(t) for t in fallidos) + "."
    return texto


def construir_detalle(
    trace_ids: list[str],
    documentos: list[Mapping[str, Any]],
    umbral_ms: int,
    correlation_id: str | None = None,
) -> DetalleTraza | None:
    tramos = _tramos(documentos)
    if not tramos:
        return None
    inicio, total_ms = _calcular_tiempos(tramos)
    entrada = next((t for t in tramos if t.padre is None and not t.externo), tramos[0])
    correlation_id = correlation_id or next((c for c in map(_correlacion, documentos) if c), None)
    return DetalleTraza(
        correlation_id=correlation_id,
        trace_ids=trace_ids,
        inicio=datetime.fromtimestamp(inicio, UTC),
        duracion_total_ms=total_ms,
        servicio_entrada=entrada.servicio,
        resultado=ESTADO_ERROR if any(t.estado == ESTADO_ERROR for t in tramos) else ESTADO_CORRECTO,
        tramos=tramos,
        interpretacion=interpretar(tramos, total_ms, umbral_ms),
        servicios_internos=len({t.servicio for t in tramos if not t.externo}),
        servicios_externos=len({t.servicio for t in tramos if t.externo}),
        contexto={"canal": ENTRADAS.get(entrada.servicio, entrada.servicio)},
    )
