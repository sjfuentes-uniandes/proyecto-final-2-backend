"""N2 (HU-W25) en INT: pagos-parametricos escala por el atraso de sus colas y drena al retirarse.

**Publican eventos sintéticos en el tópico de eventos de INT y, el respaldo, fuerza una alarma de
escalado de pagos-parametricos.** Cada fase corre solo si la expresión `-k` de la sesión contiene su
propio nombre: con el filtro vacío (todas las E2E) o con `-k hu_w25`, se omiten siempre, también con
`E2E_REQUERIDO=1`. El recorrido está en docs/plan-escalado-consumidores-hu-w25.md §f (Proyecto); la
configuración, en infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)"). No usan el portal ni el
usuario E2E, y no leen ningún secreto.

Orden: publicar → recoger enseguida (otra vez, sin goteo, a la hora que indique si no volvió al
mínimo) → solo si no escaló ni con el complemento, respaldo y otra vez recoger.

- `hu_w25_publicar`: las DLQ al empezar; un centinela que debe verse evaluado (`SIN_POLIZAS`) en la
  auditoría en 180 s (si no, falla sin publicar: hallazgo 3); la ráfaga de `RAFAGA` al inicio de un
  minuto (`PublishBatch` de `LOTE`); el ritmo por tarea de ese minuto (`NumberOfMessagesDeleted`, o
  `GetQueueAttributes` si el punto no llega en 180 s; `ritmo_por_tarea`); el complemento
  (`complemento`, plan §c.4), publicado enseguida; el muestreo de desiredCount hasta la subida o
  `TOPE_PUBLICAR_S` desde el inicio de la prueba; y la hora para recoger.
- `hu_w25_recoger`: mientras desiredCount > mínimo, el goteo de 1 evento por segundo (hasta
  `GOTEO_TOPE_S` y `GOTEO_MAXIMO`), aparte de la ráfaga y del complemento; si ya volvió, solo recoge.
  Después reconstruye con datos de AWS la línea de tiempo por minuto (`linea_de_tiempo`), comprueba
  H-3 (`coinciden_tareas`: RunningTaskCount frente a las tareas de los eventos de ECS), informa la
  tarea retirada y los liberados (Logs Insights; se informan, no se exigen) y reconcilia por
  `eventoId` (`reconciliar`). Deja el JSON en el log (`HU-W25-RECOGIDA `) y tablas en
  $GITHUB_STEP_SUMMARY.
- `hu_w25_respaldo`: solo si no escaló ni con el complemento. Fuerza a ALARM la alarma alta de la
  política -backlog (o, si ya se forzó en las últimas 3 h, la de una -antiguedad-<cola>) con un
  `StateReasonData` hecho con la plantilla de la propia alarma, como W08, y lo declara.

Guardas, antes de tocar nada (las tres fases):
1. `-k` con el nombre de la fase.
2. `ENV=int`. Las salidas de infra/platform son de int, con el clúster y el prefijo `solventa-int`.
3. boto3 con credenciales (`sts:GetCallerIdentity`).
4. El scalable target de pagos-parametricos (min < max, salida sin suspender), la política
   `<prefijo>-pagos-parametricos-backlog` con `RunningTaskCount` en su configuración (de ella sale
   T) y las `-antiguedad-<cola>` de sus dos colas. El resumen muestra `servicio al empezar:`
   (desiredCount y runningCount) también si una guarda falla.
5. publicar y respaldo: desiredCount == min y runningCount == desiredCount. publicar, además:
   RunningTaskCount con datos en los últimos 5 min y los visibles de las dos colas por tarea < T.
Si falta algo, se omite con el motivo, o falla con `E2E_REQUERIDO=1` (lo pone e2e.yml).

Datos sintéticos (plan §b-17): zona `ZONA`, sin coberturas (la decisión es `SIN_POLIZAS` y no crea
siniestros), fuente `FUENTE` y un `eventoOrigen` propio por corrida (`E2E-W25-<sufijo>`; el sufijo
empieza por la hora UTC). Las fases no comparten estado y e2e.yml no pasa variables: el `idExterno`
codifica la corrida, la clase y el total (`E2E-W25-<sufijo>-R2000-00007`; S centinela, R ráfaga, C
complemento, G goteo) y recoger reconstruye con él, desde los observados de la auditoría, qué
publicó publicar. El `eventoId` es el uuid5 de (fuente, idExterno), como el de adaptador-telemetria.
Los lotes de auditoría quedan bajo Object Lock 30 días. De las colas se imprime el nombre, no la URL.

Las funciones puras tienen sus unitarios al final (`test_hu_w25_unit_*`) y no tocan AWS.
`plantilla_de`, `claves_de` y `datos_de_estado` son copias de tests/e2e/test_hu_w08_escalado.py, y
`instante`, `cambios_de_alarma` y `_paginar`, de tests/e2e/test_hu_w09_carga.py (con
--import-mode=importlib, un archivo de pruebas no se importa desde otro).
"""

import json
import math
import os
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

import pytest

try:
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - boto3 es dependencia del workspace
    BotoCoreError = ClientError = Exception  # type: ignore[misc,assignment]

HISTORIA = "HU-W25"
AMBIENTE = "int"
CLUSTER = f"solventa-{AMBIENTE}"  # <nombre>-<ambiente>, infra/platform/main.tf:34
SERVICIO = "pagos-parametricos"
COLAS = ("parametricos-eventos", "parametricos-polizas")  # infra/platform/catalog.tf:140
COLA_RAFAGA = (
    "parametricos-eventos"  # filtro eventType = EventoParametricoObservado (catalog.tf:214)
)
COLAS_DLQ = ("parametricos-eventos", "auditoria")
CORTO = {
    "parametricos-eventos": "eventos",
    "parametricos-polizas": "polizas",
    "auditoria": "auditoria",
}
DIMENSION = "ecs:service:DesiredCount"
FASES = ("hu_w25_publicar", "hu_w25_recoger", "hu_w25_respaldo")
MOTIVOS = {
    "hu_w25_publicar": "publica la ráfaga de eventos sintéticos en el tópico de INT",
    "hu_w25_recoger": "publica el goteo y recoge la evidencia del escalado de INT",
    "hu_w25_respaldo": "fuerza una alarma de escalado de pagos-parametricos en INT",
}
RAIZ = Path(__file__).parents[2]
ESQUEMA = RAIZ / "contracts/events/EventoParametricoObservado.v1.schema.json"

# El complemento (plan §b-10 y §c.4). Constantes en el código: e2e.yml no pasa variables.
RAFAGA = 2000
VENTANA_S = 240
MARGEN = 1.2
TOPE_TOTAL = 10_000  # ráfaga más complemento, sin contar el centinela ni el goteo
LOTE = 10  # PublishBatch

FUENTE = "E2E-W25"
ZONA = "W25SINPOL"  # sin coberturas: SIN_POLIZAS, sin siniestros (plan §b-17)
TIPO_EVENTO, UNIDAD, VALOR_OBSERVADO = "LLUVIA_ACUMULADA", "mm", "1.0"
OBSERVADO = "EventoParametricoObservado"
EVALUADO = "EventoParametricoEvaluado"
DECISION_ESPERADA = "SIN_POLIZAS"
CLASES = {"S": "centinela", "R": "ráfaga", "C": "complemento", "G": "goteo"}
FUENTE_BORRADOS = "NumberOfMessagesDeleted"
FUENTE_ATRIBUTOS = "GetQueueAttributes"

# Tiempos. El job de e2e.yml corta a los 20 min (`timeout-minutes: 20`), con unos 4 de preparación.
ESPERA_CENTINELA_S = 180
ESPERA_PUNTO_S = 180
SEPARACION_CAIDA_S = 30
TOPE_PUBLICAR_S = 15 * 60  # desde el inicio de la prueba
TOPE_RECOGER_S = 15 * 60
TOPE_RESPALDO_S = 300
MUESTREO_S = 10.0
SONDEO_S = 10.0
GOTEO_INTERVALO_S = 1.0
GOTEO_TOPE_S = 600
GOTEO_MAXIMO = 800
ESPERA_AUDITORIA_S = 180
ESPERA_CONSULTA_S = 60
VENTANA_AUDITORIA = timedelta(hours=3)  # dónde busca recoger la corrida de publicar
ANTES_DE_LA_RAFAGA = timedelta(minutes=5)
PUNTOS_REDUCCION = 15  # target tracking reduce con 15 puntos de 1 min bajo el 90 % de T
UMBRAL_REDUCCION = 0.9
MARGEN_RECOGER_S = 120
STOP_TIMEOUT_S = 30  # infra/apps/ecs.tf:165
MARGEN_RETIRO_S = 60
TOLERANCIA_TAREAS = 1
MAX_MINUTOS_DISCREPANCIA = 2
HILOS = 16
MAX_PAGINAS = 50
LIMITE_RESUMEN = 1_000_000  # $GITHUB_STEP_SUMMARY admite 1 MiB por paso
PREFIJO_PUBLICACION = "HU-W25-PUBLICACION"
PREFIJO_RECOGIDA = "HU-W25-RECOGIDA"
PREFIJO_RESPALDO = "HU-W25-RESPALDO"
RAZON_ALARMA = "HU-W25 N2 (respaldo): alarma alta forzada para demostrar el escalado hacia fuera"
MENSAJE_LIBERADO = "inbox: mensaje liberado por la parada"  # solventa_common/inbox.py:128
# App Auto Scaling nombra las alarmas de target tracking
# `TargetTracking-<ResourceId>-AlarmHigh-<uuid>` y `...-AlarmLow-<uuid>`.
_ALARMA = re.compile(r"-Alarm(High|Low)-[0-9A-Fa-f-]+$")
_DESEADAS = re.compile(r"desired count to (\d+)", re.IGNORECASE)
_POLITICA = re.compile(r"triggered policy (\S+)")
_ESTADO_ALARMA = re.compile(r"to (OK|ALARM|INSUFFICIENT_DATA)\b")
_ID_EXTERNO = re.compile(
    r"E2E-W25-(?P<sufijo>[0-9a-z]{18})-(?P<clase>[SRCG])(?P<total>[1-9][0-9]*)?-(?P<n>[0-9]{5})"
)


# --- Funciones puras (con unitarios al final) --------------------------------------------------


def seleccionada(keyword: str | None, nombre: str) -> bool:
    """Si la sesión eligió esta fase a propósito: `-k` con su propio nombre."""
    return nombre in (keyword or "")


def instante(valor: Any) -> datetime:
    """Una fecha de AWS (datetime o ISO 8601, con o sin Z) como datetime en UTC."""
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor)
        except ValueError:
            raise ValueError(f"fecha no reconocida: {valor!r}") from None
    if isinstance(valor, datetime):
        return (valor if valor.tzinfo else valor.replace(tzinfo=UTC)).astimezone(UTC)
    raise ValueError(f"fecha no reconocida: {valor!r}")


def _con_zona(*momentos: datetime | None) -> None:
    if any(m is not None and m.tzinfo is None for m in momentos):
        raise ValueError("las horas necesitan zona horaria")


def _minuto_abajo(t: datetime) -> datetime:
    return instante(t).replace(second=0, microsecond=0)


def _minuto_arriba(t: datetime) -> datetime:
    abajo = _minuto_abajo(t)
    return abajo if abajo == instante(t) else abajo + timedelta(minutes=1)


def _iso_z(t: datetime) -> str:
    return instante(t).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def nuevo_sufijo(ahora: datetime) -> str:
    """La hora UTC (14 dígitos, ordena las corridas) y 4 hexadecimales al azar."""
    return f"{instante(ahora):%Y%m%d%H%M%S}{uuid.uuid4().hex[:4]}"


def id_externo(sufijo: str, clase: str, total: int | None, n: int) -> str:
    """`E2E-W25-<sufijo>-<clase><total>-<n>`: el n-ésimo evento (desde 0) de una clase con su total
    (S, R y C); el goteo (G) va sin total, porque se publica mientras dura la reducción."""
    if clase not in CLASES:
        raise ValueError(f"clase {clase!r}: debe ser una de {sorted(CLASES)}")
    if (clase == "G") != (total is None):
        raise ValueError(f"la clase {clase} {'no ' if clase == 'G' else ''}lleva total")
    if total is not None and total < 1:
        raise ValueError(f"total {total}: hace falta al menos 1")
    if n < 0 or (total is not None and n >= total):
        raise ValueError(f"n = {n} fuera de [0, {total})")
    marca = clase if total is None else f"{clase}{total}"
    return f"E2E-W25-{sufijo}-{marca}-{n:05d}"


@dataclass(frozen=True)
class IdExterno:
    sufijo: str
    clase: str
    total: int | None
    n: int


def leer_id_externo(valor: str) -> IdExterno | None:
    """El `idExterno` de un evento de W25, o None si no lo es."""
    encontrado = _ID_EXTERNO.fullmatch(valor or "")
    if not encontrado:
        return None
    clase, total = encontrado["clase"], encontrado["total"]
    if (clase == "G") != (total is None):
        return None
    return IdExterno(
        encontrado["sufijo"], clase, int(total) if total else None, int(encontrado["n"])
    )


def evento_id_de(valor: str) -> str:
    """El uuid5 de (fuente, idExterno), con la fórmula de adaptador-telemetria
    (domain/telemetria/modelos.py:12)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"telemetria/{FUENTE}/{valor}"))


def sobre_observado(valor: str, sufijo: str, ahora: datetime) -> dict[str, Any]:
    """El sobre `EventoParametricoObservado` v1 del evento `valor` (su `idExterno`), con `eventId` =
    `aggregateId` = `eventoId` (el manejador lo exige; `auditoria`, `occurredAt` y `aggregateId`)."""
    _con_zona(ahora)
    evento_id = evento_id_de(valor)
    momento = _iso_z(ahora)
    return {
        "eventId": evento_id,
        "aggregateId": evento_id,
        "type": OBSERVADO,
        "version": 1,
        "occurredAt": momento,
        "correlationId": f"e2e-w25-{sufijo}",
        "payload": {
            "eventoId": evento_id,
            "fuente": FUENTE,
            "idExterno": valor,
            "eventoOrigen": f"E2E-W25-{sufijo}",
            "tipoEvento": TIPO_EVENTO,
            "valorObservado": VALOR_OBSERVADO,
            "unidad": UNIDAD,
            "zona": ZONA,
            "observadoEn": momento,
            "recibidoEn": momento,
            "archivoRef": f"e2e-w25/{instante(ahora):%Y/%m/%d}/{evento_id}.json",
        },
    }


def lotes(elementos: Sequence[Any], tamano: int = LOTE) -> list[list[Any]]:
    """`elementos` en lotes de `tamano` (el último, con lo que quede), en orden."""
    if tamano < 1:
        raise ValueError(f"tamaño de lote {tamano}: hace falta al menos 1")
    lista = list(elementos)
    return [lista[i : i + tamano] for i in range(0, len(lista), tamano)]


@dataclass(frozen=True)
class Caida:
    """Dos lecturas de `ApproximateNumberOfMessages` separadas `segundos`; la primera, a
    `desde_rafaga_s` del inicio de la ráfaga."""

    antes: int
    despues: int
    segundos: float
    desde_rafaga_s: float


@dataclass(frozen=True)
class Ritmo:
    valor: float  # mensajes por segundo por tarea
    fuente: str
    cota_inferior: bool  # la cola llegó a 0: el ritmo real puede ser mayor
    calculo: str


def ritmo_por_tarea(
    tareas: int,
    borrados_minuto: float | None,
    visibles_al_cerrar: int | None,
    caida: Caida | None = None,
    publicados: int = RAFAGA,
) -> Ritmo:
    """El ritmo de consumo por tarea en el primer minuto de la ráfaga (plan §c.4, punto 2).

    Con el punto de `NumberOfMessagesDeleted` de ese minuto: borrados / 60 / tareas, cota inferior si
    la cola estaba en 0 al cerrar el minuto. Sin el punto: si la cola estaba en 0 al cerrar,
    publicados / 60 / tareas (cota); si no, la caída entre las dos lecturas de `caida` (cota si la
    segunda es 0) o, si la primera ya era 0, publicados / el tiempo desde la ráfaga (cota).

    `ValueError` sin tareas o sin ningún dato."""
    if tareas < 1:
        raise ValueError(f"tareas = {tareas}: hace falta al menos 1")
    if borrados_minuto is not None:
        return Ritmo(
            borrados_minuto / 60 / tareas,
            FUENTE_BORRADOS,
            visibles_al_cerrar == 0,
            f"{borrados_minuto:g} borrados en el primer minuto / 60 s / {tareas} tarea(s)",
        )
    if visibles_al_cerrar == 0:
        return Ritmo(
            publicados / 60 / tareas,
            FUENTE_ATRIBUTOS,
            True,
            f"la cola se vació en el primer minuto: {publicados} / 60 s / {tareas} tarea(s)",
        )
    if caida is not None:
        if caida.segundos <= 0 or caida.desde_rafaga_s <= 0:
            raise ValueError(f"lecturas sin separación: {caida}")
        if caida.antes == 0:
            return Ritmo(
                publicados / caida.desde_rafaga_s / tareas,
                FUENTE_ATRIBUTOS,
                True,
                f"la cola ya estaba vacía: {publicados} / {caida.desde_rafaga_s:g} s desde la "
                f"ráfaga / {tareas} tarea(s)",
            )
        return Ritmo(
            max(0, caida.antes - caida.despues) / caida.segundos / tareas,
            FUENTE_ATRIBUTOS,
            caida.despues == 0,
            f"({caida.antes} − {caida.despues}) visibles / {caida.segundos:g} s / {tareas} tarea(s)",
        )
    raise ValueError("sin el punto de NumberOfMessagesDeleted ni lecturas de GetQueueAttributes")


@dataclass(frozen=True)
class Complemento:
    eventos: int
    necesario: float | None  # antes de redondear y del tope; None si no hizo falta calcularlo
    tope_aplicado: bool
    motivo: str
    calculo: str


def complemento(ritmo: float, objetivo: float, visibles_ahora: int) -> Complemento:
    """Los eventos que hay que publicar enseguida para que los visibles por tarea sigan sobre T
    unos `VENTANA_S` (plan §c.4, puntos 3 y 4).

    Ninguno si la ráfaga sola alcanza ((RAFAGA − T) / c ≥ VENTANA_S) o si c = 0 (la cola no se
    mueve). Si no, c · VENTANA_S · MARGEN + T − visibles_ahora, redondeado hacia arriba al múltiplo
    de LOTE, sin pasar de TOPE_TOTAL − RAFAGA y nunca negativo. `ValueError` con un ritmo negativo,
    un objetivo que no es positivo o visibles negativos."""
    if ritmo < 0 or objetivo <= 0 or visibles_ahora < 0:
        raise ValueError(f"c = {ritmo}, T = {objetivo}, visibles_ahora = {visibles_ahora}")
    tope = TOPE_TOTAL - RAFAGA
    if ritmo == 0:
        return Complemento(0, None, False, "la cola no se mueve: se sostiene sola", "c = 0")
    duracion = (RAFAGA - objetivo) / ritmo
    if duracion >= VENTANA_S:
        return Complemento(
            0,
            None,
            False,
            "la ráfaga sola alcanza",
            f"(ráfaga {RAFAGA} − T {objetivo:g}) / c {ritmo:.3f} = {duracion:.1f} s ≥ {VENTANA_S} s",
        )
    necesario = round(ritmo * VENTANA_S * MARGEN + objetivo - visibles_ahora, 6)
    formula = (
        f"(ráfaga {RAFAGA} − T {objetivo:g}) / c {ritmo:.3f} = {duracion:.1f} s < {VENTANA_S} s; "
        f"c {ritmo:.3f} × {VENTANA_S} s × margen {MARGEN} + T {objetivo:g} − visibles_ahora "
        f"{visibles_ahora} = {necesario:.1f}"
    )
    if necesario <= 0:
        return Complemento(0, necesario, False, "lo que queda en la cola ya alcanza", formula)
    redondeado = math.ceil(necesario / LOTE) * LOTE
    eventos = min(tope, redondeado)
    return Complemento(
        eventos,
        necesario,
        redondeado > tope,
        "con complemento" + (f" (tope: {TOPE_TOTAL} en total)" if redondeado > tope else ""),
        f"{formula} → {redondeado} (múltiplo de {LOTE}) → min({tope}, {redondeado}) = {eventos}",
    )


def linea_de_tiempo(
    desde: datetime,
    hasta: datetime,
    series: Mapping[str, Mapping[datetime, float]],
    cambios: Mapping[str, Iterable[tuple[datetime, Any]]],
) -> list[dict[str, Any]]:
    """Una fila por minuto de [desde, hasta): el valor de cada serie en ese minuto (None si no hay
    punto) y, de cada lista de cambios (hora, valor), el vigente al cerrar el minuto."""
    _con_zona(desde, hasta)
    puntos = {nombre: {_minuto_abajo(t): v for t, v in s.items()} for nombre, s in series.items()}
    ordenados = {
        nombre: sorted(((instante(t), v) for t, v in lista), key=lambda c: c[0])
        for nombre, lista in cambios.items()
    }
    filas: list[dict[str, Any]] = []
    minuto = _minuto_abajo(desde)
    while minuto < hasta:
        cierre = minuto + timedelta(minutes=1)
        fila: dict[str, Any] = {"minuto": minuto}
        for nombre, serie in puntos.items():
            fila[nombre] = serie.get(minuto)
        for nombre, lista in ordenados.items():
            fila[nombre] = next((v for t, v in reversed(lista) if t < cierre), None)
        filas.append(fila)
        minuto = cierre
    return filas


def _detalle_propio(evento: dict[str, Any]) -> dict[str, Any] | None:
    """El `detail` de un `ECS Task State Change` de una tarea de pagos-parametricos."""
    if evento.get("detail-type") != "ECS Task State Change":
        return None
    detalle = evento.get("detail") or {}
    if detalle.get("group") != f"service:{SERVICIO}" or not detalle.get("taskArn"):
        return None
    return dict(detalle)


def tareas_de_eventos(
    eventos: Iterable[dict[str, Any]], desde: datetime, hasta: datetime
) -> dict[datetime, int]:
    """Por minuto de [desde, hasta), las tareas de pagos-parametricos en marcha a mitad del minuto
    según los eventos `ECS Task State Change` (desde `startedAt` hasta `stoppingAt` o `stoppedAt`)."""
    inicio: dict[str, datetime] = {}
    fin: dict[str, datetime] = {}
    for evento in eventos:
        detalle = _detalle_propio(evento)
        if detalle is None:
            continue
        tarea = str(detalle["taskArn"]).rsplit("/", 1)[-1]
        if detalle.get("startedAt"):
            empezo = instante(detalle["startedAt"])
            inicio[tarea] = min(inicio.get(tarea, empezo), empezo)
        parada = detalle.get("stoppingAt") or detalle.get("stoppedAt")
        if parada:
            paro = instante(parada)
            fin[tarea] = min(fin.get(tarea, paro), paro)
    resultado: dict[datetime, int] = {}
    minuto = _minuto_abajo(desde)
    while minuto < hasta:
        medio = minuto + timedelta(seconds=30)
        resultado[minuto] = sum(
            1
            for tarea, t in inicio.items()
            if t <= medio and (tarea not in fin or medio < fin[tarea])
        )
        minuto += timedelta(minutes=1)
    return resultado


@dataclass(frozen=True)
class Coincidencia:
    ok: bool
    discrepancias: list[tuple[datetime, float, float]]  # (minuto, RunningTaskCount, eventos)
    racha_maxima: int
    sin_datos: int
    comparados: int


def coinciden_tareas(
    running: Mapping[datetime, float],
    eventos: Mapping[datetime, float],
    tolerancia: float = TOLERANCIA_TAREAS,
    max_minutos: int = MAX_MINUTOS_DISCREPANCIA,
) -> Coincidencia:
    """La comprobación de H-3: RunningTaskCount (Average por minuto) frente a las tareas de los
    eventos de ECS. Falla si difieren en más de `tolerancia` durante más de `max_minutos` minutos
    seguidos. Un minuto sin alguno de los dos datos corta la racha y se cuenta aparte."""
    racha = maxima = sin_datos = comparados = 0
    discrepancias: list[tuple[datetime, float, float]] = []
    for minuto in sorted(set(running) | set(eventos)):
        a, b = running.get(minuto), eventos.get(minuto)
        if a is None or b is None:
            sin_datos += 1
            racha = 0
            continue
        comparados += 1
        if abs(a - b) > tolerancia:
            racha += 1
            maxima = max(maxima, racha)
            discrepancias.append((minuto, a, b))
        else:
            racha = 0
    return Coincidencia(maxima <= max_minutos, discrepancias, maxima, sin_datos, comparados)


@dataclass(frozen=True)
class Reconciliacion:
    publicados: int
    evaluados: int  # publicados con al menos un EventoParametricoEvaluado
    faltantes: list[str]
    repetidos: dict[str, int]
    sin_observado: list[str]  # se informa: publicados sin su observado en la auditoría
    delta_dlq: dict[str, int]
    ok: bool


def reconciliar(
    esperados: Iterable[str],
    observados: Iterable[str],
    evaluados: Iterable[str],
    dlq_antes: Mapping[str, int],
    dlq_despues: Mapping[str, int],
) -> Reconciliacion:
    """Por `eventoId` (plan §b-13): exactamente un evaluado por cada publicado y cero en el delta de
    cada DLQ. Lo ajeno a `esperados` se ignora. `ValueError` sin esperados o con DLQ distintas."""
    lista = list(dict.fromkeys(esperados))
    if not lista:
        raise ValueError("no hay eventos publicados que reconciliar")
    if set(dlq_antes) != set(dlq_despues):
        raise ValueError(f"DLQ distintas: {sorted(dlq_antes)} y {sorted(dlq_despues)}")
    propios = set(lista)
    cuenta = Counter(e for e in evaluados if e in propios)
    vistos = set(observados)
    faltantes = [e for e in lista if cuenta[e] == 0]
    repetidos = {e: n for e, n in cuenta.items() if n > 1}
    delta = {cola: int(dlq_despues[cola]) - int(dlq_antes[cola]) for cola in dlq_antes}
    return Reconciliacion(
        len(lista),
        len(lista) - len(faltantes),
        faltantes,
        repetidos,
        [e for e in lista if e not in vistos],
        delta,
        not faltantes and not repetidos and all(d <= 0 for d in delta.values()),
    )


def ventana_de_retiro(
    reduccion: datetime, detenidas: Iterable[tuple[datetime | None, datetime | None]]
) -> tuple[datetime, datetime]:
    """Dónde buscar los liberados del retiro: desde la reducción (o la primera parada, si fue antes)
    menos `MARGEN_RETIRO_S`, hasta la última tarea detenida más el margen; sin tareas detenidas,
    hasta la reducción (o la última parada) más `STOP_TIMEOUT_S` y el margen."""
    _con_zona(reduccion)
    lista = list(detenidas)
    paradas = [p for p, _ in lista if p is not None]
    finales = [d for _, d in lista if d is not None]
    margen = timedelta(seconds=MARGEN_RETIRO_S)
    desde = min([reduccion, *paradas]) - margen
    if finales:
        return desde, max(finales) + margen
    return desde, max([reduccion, *paradas]) + timedelta(seconds=STOP_TIMEOUT_S) + margen


def hora_para_recoger(
    ahora: datetime,
    bajo_objetivo_desde: datetime | None,
    ultima_reduccion: datetime | None,
    enfriamiento_entrada_s: int,
) -> datetime:
    """Desde cuándo tiene sentido recoger la vuelta al mínimo: `PUNTOS_REDUCCION` minutos desde que
    los visibles por tarea bajaron del 90 % de T (o desde ahora, si no se sabe) y, tras una reducción
    parcial, su enfriamiento de entrada; más `MARGEN_RECOGER_S`. Nunca antes de ahora, en minutos."""
    _con_zona(ahora, bajo_objetivo_desde, ultima_reduccion)
    margen = timedelta(seconds=MARGEN_RECOGER_S)
    candidata = (bajo_objetivo_desde or ahora) + timedelta(minutes=PUNTOS_REDUCCION) + margen
    if ultima_reduccion is not None:
        candidata = max(
            candidata, ultima_reduccion + timedelta(seconds=enfriamiento_entrada_s) + margen
        )
    return _minuto_arriba(max(ahora, candidata))


@dataclass(frozen=True)
class Politica:
    nombre: str
    objetivo: float
    enfriamiento_salida: int | None
    enfriamiento_entrada: int | None
    desactiva_entrada: bool
    metricas: tuple[str, ...]
    alarma_alta: str
    alarma_baja: str | None

    @property
    def por_tarea(self) -> bool:
        return "RunningTaskCount" in self.metricas


def politica_de(respuesta: dict[str, Any], nombre: str, exige_baja: bool = True) -> Politica:
    """La política `nombre` de una respuesta de `DescribeScalingPolicies`, con las métricas de su
    configuración y sus alarmas tal como las devuelve la API (por el sufijo `-AlarmHigh-` o
    `-AlarmLow-`; el nombre nunca se construye). Una política solo de salida (`DisableScaleIn`) puede
    no tener alarma baja: `exige_baja=False`.

    `LookupError` si no está una sola vez; `ValueError` si no es de target tracking o sus alarmas no
    son una alta y (con `exige_baja`) una baja."""
    candidatas = [p for p in respuesta.get("ScalingPolicies", []) if p.get("PolicyName") == nombre]
    if len(candidatas) != 1:
        raise LookupError(f"{len(candidatas)} políticas {nombre!r} en la respuesta")
    politica = candidatas[0]
    if politica.get("PolicyType") != "TargetTrackingScaling":
        raise ValueError(f"{nombre!r} es {politica.get('PolicyType')!r}, no TargetTrackingScaling")
    altas: list[str] = []
    bajas: list[str] = []
    for alarma in politica.get("Alarms") or []:
        nombre_alarma = str(alarma.get("AlarmName", ""))
        encontrada = _ALARMA.search(nombre_alarma)
        if encontrada:
            (altas if encontrada.group(1) == "High" else bajas).append(nombre_alarma)
    if len(altas) != 1 or len(bajas) > 1 or (exige_baja and len(bajas) != 1):
        raise ValueError(f"alarmas de {nombre!r}: altas {altas}, bajas {bajas}")
    config = politica.get("TargetTrackingScalingPolicyConfiguration") or {}
    especificacion = config.get("CustomizedMetricSpecification") or {}
    metricas = tuple(
        nombre_metrica
        for m in especificacion.get("Metrics") or []
        if (nombre_metrica := ((m.get("MetricStat") or {}).get("Metric") or {}).get("MetricName"))
    )
    if not metricas and especificacion.get("MetricName"):
        metricas = (str(especificacion["MetricName"]),)
    return Politica(
        nombre,
        float(config["TargetValue"]),
        config.get("ScaleOutCooldown"),
        config.get("ScaleInCooldown"),
        bool(config.get("DisableScaleIn", False)),
        metricas,
        altas[0],
        bajas[0] if bajas else None,
    )


@dataclass(frozen=True)
class Contexto:
    cluster: str
    prefijo: str
    region: str
    topico: str
    bucket: str
    eventos_ecs: str  # events_log_group: los eventos de ECS (infra/platform/observability.tf:43)
    colas: dict[str, dict[str, str]]  # nombre corto del catálogo → name, url y dlq_name


def contexto_de(salidas: dict[str, Any]) -> Contexto:
    """Lo que las fases necesitan de las salidas de infra/platform (objeto `platform`), solo de INT.
    `ValueError` con lo que falte o si son de otro ambiente."""
    plataforma = salidas.get("platform") or {}
    if not plataforma:
        raise ValueError("faltan las salidas de infra/platform (terraform output)")
    if plataforma.get("environment") != AMBIENTE:
        raise ValueError(
            f"las salidas de infra/platform son de {plataforma.get('environment')!r}, no de int"
        )
    cluster = (plataforma.get("cluster") or {}).get("name")
    prefijo = plataforma.get("prefix")
    if cluster != CLUSTER or prefijo != CLUSTER:
        raise ValueError(f"clúster o prefijo inesperados para int: {cluster!r}, {prefijo!r}")
    topico = plataforma.get("events_topic_arn")
    bucket = (plataforma.get("audit_bucket") or {}).get("name")
    eventos = plataforma.get("events_log_group")
    faltan = [
        nombre
        for nombre, valor in (
            ("events_topic_arn", topico),
            ("audit_bucket.name", bucket),
            ("events_log_group", eventos),
        )
        if not valor
    ]
    colas = plataforma.get("queues") or {}
    for cola in (*COLAS, "auditoria"):
        datos = colas.get(cola) or {}
        if not all(datos.get(clave) for clave in ("name", "url", "dlq_name")):
            faltan.append(f"queues.{cola}")
    if faltan:
        raise ValueError(f"faltan salidas de infra/platform: {', '.join(faltan)}")
    return Contexto(
        str(cluster),
        str(prefijo),
        str(plataforma.get("region") or os.getenv("AWS_REGION", "us-east-1")),
        str(topico),
        str(bucket),
        str(eventos),
        {
            cola: {clave: str(colas[cola][clave]) for clave in ("name", "url", "dlq_name")}
            for cola in (*COLAS, "auditoria")
        },
    )


@dataclass
class Corrida:
    sufijo: str
    declarados: dict[str, int] = field(default_factory=dict)  # clase → total del idExterno
    observados: dict[str, set[str]] = field(default_factory=dict)  # clase → idExterno vistos
    primeras: dict[str, datetime] = field(default_factory=dict)  # clase → primer occurredAt


def corridas_de(registros: Iterable[dict[str, Any]]) -> dict[str, Corrida]:
    """Las corridas de W25 de los registros de auditoría: por sufijo, lo que declaran sus `idExterno`
    y lo que se observó (registros `EventoParametricoObservado` con fuente `FUENTE`)."""
    corridas: dict[str, Corrida] = {}
    for registro in registros:
        if registro.get("accion") != OBSERVADO:
            continue
        evento = registro.get("evento")
        if isinstance(evento, str):
            try:
                evento = json.loads(evento)
            except ValueError:
                continue
        if not isinstance(evento, dict):
            continue
        payload = evento.get("payload")
        if not isinstance(payload, dict) or payload.get("fuente") != FUENTE:
            continue
        valor = str(payload.get("idExterno") or "")
        leido = leer_id_externo(valor)
        if leido is None:
            continue
        corrida = corridas.setdefault(leido.sufijo, Corrida(leido.sufijo))
        if leido.total is not None:
            corrida.declarados[leido.clase] = leido.total
        corrida.observados.setdefault(leido.clase, set()).add(valor)
        try:
            momento = instante(registro.get("timestamp") or evento.get("occurredAt"))
        except ValueError:
            continue
        if leido.clase not in corrida.primeras or momento < corrida.primeras[leido.clase]:
            corrida.primeras[leido.clase] = momento
    return corridas


def esperados_de(corridas: Mapping[str, Corrida]) -> tuple[Corrida, list[Corrida], list[str]]:
    """La corrida de publicar (la más reciente con ráfaga), los goteos posteriores y los `eventoId`
    que se publicaron: los de publicar, enteros por su total declarado (centinela, ráfaga y
    complemento); los del goteo, los observados (su total no se declara). `LookupError` si no hay
    ninguna corrida con ráfaga."""
    publicadas = [c for c in corridas.values() if "R" in c.declarados]
    if not publicadas:
        raise LookupError("no hay ninguna corrida de hu_w25_publicar (con ráfaga)")
    publicar = max(publicadas, key=lambda c: c.sufijo)
    goteos = sorted(
        (c for c in corridas.values() if c.sufijo > publicar.sufijo and set(c.observados) == {"G"}),
        key=lambda c: c.sufijo,
    )
    ids: list[str] = []
    for clase in ("S", "R", "C"):
        total = publicar.declarados.get(clase)
        if total:
            ids += [
                evento_id_de(id_externo(publicar.sufijo, clase, total, n)) for n in range(total)
            ]
    for goteo in goteos:
        ids += [evento_id_de(v) for v in sorted(goteo.observados["G"])]
    return publicar, goteos, ids


@dataclass(frozen=True)
class Actividad:
    inicio: datetime
    fin: datetime | None
    estado: str
    deseadas: int | None
    politica: str | None
    descripcion: str


def actividades_de(items: Iterable[dict[str, Any]]) -> list[Actividad]:
    """Las actividades de `DescribeScalingActivities`, en orden, con el desiredCount que fijaron y la
    política que las pidió (de su `Cause`)."""
    actividades = []
    for item in items:
        deseadas = _DESEADAS.search(str(item.get("Description", "")))
        politica = _POLITICA.search(str(item.get("Cause", "")))
        actividades.append(
            Actividad(
                instante(item["StartTime"]),
                instante(item["EndTime"]) if item.get("EndTime") else None,
                str(item.get("StatusCode", "")),
                int(deseadas.group(1)) if deseadas else None,
                politica.group(1) if politica else None,
                str(item.get("Description", "")),
            )
        )
    return sorted(actividades, key=lambda a: a.inicio)


def cambios_de_capacidad(actividades: Iterable[Actividad], inicial: int) -> list[dict[str, Any]]:
    """Las actividades exitosas que cambiaron desiredCount, con de cuántas a cuántas tareas, el
    sentido (subida o reducción) y la política, partiendo de `inicial`."""
    cambios = []
    actual = inicial
    for actividad in actividades:
        if actividad.estado != "Successful" or actividad.deseadas is None:
            continue
        if actividad.deseadas != actual:
            cambios.append(
                {
                    "hora": actividad.inicio,
                    "de": actual,
                    "a": actividad.deseadas,
                    "sentido": "subida" if actividad.deseadas > actual else "reducción",
                    "politica": actividad.politica,
                }
            )
            actual = actividad.deseadas
    return cambios


def cambios_de_alarma(items: Iterable[dict[str, Any]]) -> list[tuple[datetime, str]]:
    """(hora, estado nuevo) de cada `StateUpdate` de DescribeAlarmHistory, en orden."""
    cambios = []
    for item in items:
        try:
            estado = (json.loads(item.get("HistoryData") or "").get("newState") or {}).get(
                "stateValue"
            )
        except (ValueError, AttributeError, TypeError):
            estado = None
        if not estado:
            encontrado = _ESTADO_ALARMA.search(str(item.get("HistorySummary", "")))
            estado = encontrado.group(1) if encontrado else None
        if estado:
            cambios.append((instante(item["Timestamp"]), str(estado)))
    return sorted(cambios, key=lambda c: c[0])


def tareas_detenidas(eventos: Iterable[dict[str, Any]], desde: datetime) -> list[dict[str, Any]]:
    """Las tareas de pagos-parametricos detenidas desde `desde`, de los eventos `ECS Task State
    Change` (o de DescribeTasks con la misma forma): parada (`stoppingAt`), detenida (`stoppedAt`),
    cuánto tardó, `stoppedReason`, `stopCode` y el `exitCode` de cada contenedor."""
    por_tarea: dict[str, dict[str, Any]] = {}
    for evento in eventos:
        detalle = _detalle_propio(evento)
        if (
            detalle is None
            or detalle.get("lastStatus") != "STOPPED"
            or not detalle.get("stoppedAt")
        ):
            continue
        detenida = instante(detalle["stoppedAt"])
        if detenida < desde:
            continue
        tarea = str(detalle["taskArn"]).rsplit("/", 1)[-1]
        anterior = por_tarea.get(tarea, {})
        parada = instante(detalle["stoppingAt"]) if detalle.get("stoppingAt") else None
        parada = parada or anterior.get("parada")
        salidas = {c.get("name"): c.get("exitCode") for c in detalle.get("containers") or []}
        por_tarea[tarea] = {
            "id": tarea,
            "parada": parada,
            "detenida": detenida,
            "duracion_s": round((detenida - parada).total_seconds(), 1) if parada else None,
            "stoppedReason": detalle.get("stoppedReason") or anterior.get("stoppedReason"),
            "stopCode": detalle.get("stopCode") or anterior.get("stopCode"),
            "exitCode": salidas or anterior.get("exitCode") or {},
        }
    return sorted(por_tarea.values(), key=lambda t: t["detenida"])


def bajo_objetivo_desde(
    filas: Sequence[dict[str, Any]],
    objetivo: float,
    visibles: Sequence[str] = ("visibles_eventos", "visibles_polizas"),
    tareas: str = "rtc",
) -> datetime | None:
    """El primer minuto con datos después del último en que los visibles por tarea estuvieron en el
    90 % de T o más: desde ahí cuentan los puntos de la reducción. None si nunca llegaron al 90 % o
    si siguen ahí."""
    umbral = UMBRAL_REDUCCION * objetivo

    def por_tarea(fila: dict[str, Any]) -> float | None:
        valores = [fila.get(c) for c in visibles]
        n = fila.get(tareas)
        if any(v is None for v in valores) or not n:
            return None
        return float(sum(valores)) / float(n)  # type: ignore[arg-type]

    ultimo = None
    for i, fila in enumerate(filas):
        valor = por_tarea(fila)
        if valor is not None and valor >= umbral:
            ultimo = i
    if ultimo is None:
        return None
    return next((f["minuto"] for f in filas[ultimo + 1 :] if por_tarea(f) is not None), None)


def valor_para_deseadas(umbral: float, tareas: int, deseadas: int) -> float:
    """El valor que se informa al forzar la alarma alta para que target tracking pida `deseadas`
    tareas desde `tareas`: capacidad × valor / objetivo = deseadas − 0,25, que da `deseadas` tanto si
    redondea hacia arriba como al más cercano (W08 lo usa con deseadas = tareas + 1)."""
    if tareas < 1 or deseadas <= tareas:
        raise ValueError(f"hacen falta tareas >= 1 y deseadas > tareas ({tareas}, {deseadas})")
    return umbral * (deseadas - 0.25) / tareas


def elegir_alarma(
    candidatas: Sequence[tuple[str, str]], forzadas: Iterable[str]
) -> tuple[str, str]:
    """La primera (política, alarma alta) que no se forzó todavía; si ya se forzaron todas, la
    primera (la de -backlog). `ValueError` sin candidatas."""
    if not candidatas:
        raise ValueError("no hay alarmas que forzar")
    ya = set(forzadas)
    return next((c for c in candidatas if c[1] not in ya), candidatas[0])


def _celda(valor: Any) -> str:
    if valor is None:
        return "—"
    if isinstance(valor, datetime):
        return f"{instante(valor):%H:%M}"
    if isinstance(valor, float):
        return f"{round(valor, 2):g}"
    return str(valor).replace("|", "\\|").replace("\n", " ")


def tabla(encabezados: Sequence[str], filas: Iterable[Iterable[Any]]) -> str:
    """Una tabla Markdown para $GITHUB_STEP_SUMMARY."""
    lineas = [
        "| " + " | ".join(encabezados) + " |",
        "| " + " | ".join("---" for _ in encabezados) + " |",
    ]
    lineas += ["| " + " | ".join(_celda(c) for c in fila) + " |" for fila in filas]
    return "\n".join(lineas)


def limitar(texto: str, limite: int = LIMITE_RESUMEN) -> str:
    nota = "\n(recortado: el JSON completo está en el log del job)"
    if len(texto.encode("utf-8")) <= limite:
        return texto
    return texto.encode("utf-8")[: limite - len(nota.encode())].decode("utf-8", "ignore") + nota


def _fecha_aws(momento: datetime) -> str:
    """`2018-10-31T14:32:52.031+0000`, el formato del ejemplo de `SetAlarmState`."""
    utc = momento.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}+0000"


# Formato por omisión del `StateReasonData`: el que escribe CloudWatch al evaluar una alarma de una
# métrica (camelCase minúscula, con `evaluatedDatapoints`; guía de CloudWatch "Alarm actions"). Solo
# importan las claves y su orden: los valores los pone `datos_de_estado`, salvo `version`.
FORMATO_POR_OMISION: dict[str, Any] = {
    "version": "1.0",
    "queryDate": None,
    "startDate": None,
    "statistic": None,
    "period": None,
    "recentDatapoints": None,
    "threshold": None,
    "evaluatedDatapoints": [],
}
MAX_DATOS_DE_ESTADO = 4000  # longitud máxima de `StateReasonData` en `SetAlarmState`


def plantilla_de(alarma: dict[str, Any]) -> dict[str, Any] | None:
    """El `StateReasonData` que CloudWatch dejó en `alarma` (la de `DescribeAlarms`) al evaluarla,
    como diccionario; `None` si falta, no es JSON, no es un objeto o no trae `recentDatapoints`
    (sin esa clave el valor forzado no viajaría)."""
    texto = alarma.get("StateReasonData")
    if not texto:
        return None
    try:
        datos = json.loads(texto)
    except (TypeError, ValueError):
        return None
    if not isinstance(datos, dict) or not any(k.lower() == "recentdatapoints" for k in datos):
        return None
    return datos


def claves_de(plantilla: dict[str, Any] | None) -> list[str] | None:
    """Las claves de `plantilla`, sin sus valores (las de una lista de objetos, entre corchetes)."""
    if plantilla is None:
        return None
    return [
        f"{clave}[{', '.join(valor[0])}]"
        if isinstance(valor, list) and valor and isinstance(valor[0], dict)
        else clave
        for clave, valor in plantilla.items()
    ]


def _clave(datos: dict[str, Any], nombre: str) -> str:
    """La clave de `datos` que es `nombre` sin distinguir mayúsculas, o `nombre` si no está."""
    return next((k for k in datos if k.lower() == nombre.lower()), nombre)


def _punto(plantilla: Any, momento: str, valor: float) -> dict[str, Any]:
    """Un punto de `evaluatedDatapoints` con las claves del de `plantilla` (si es un objeto)."""
    punto = dict(plantilla) if isinstance(plantilla, dict) else {}
    punto[_clave(punto, "timestamp")] = momento
    muestras = _clave(punto, "sampleCount")
    punto[muestras] = punto.get(muestras, 1.0)
    punto[_clave(punto, "value")] = valor
    return punto


def datos_de_estado(alarma: dict[str, Any], ahora: datetime, valor: float) -> str:
    """`StateReasonData` para forzar `alarma` (la de `DescribeAlarms`) a `ALARM` con `valor`.

    App Auto Scaling lo exige cuando la alarma invoca una política y calcula el ajuste con él; con
    las claves del ejemplo de la API `SetAlarmState` no ejecuta la acción (N2 de W08 del
    2026-11-22). Por eso la plantilla es el `StateReasonData` que CloudWatch dejó en la alarma al
    evaluarla (`plantilla_de`) o, si no sirve, `FORMATO_POR_OMISION`: se conservan sus claves y se
    reemplazan solo los valores. `queryDate` es `ahora`; `startDate`, el inicio del periodo anterior
    al de la consulta; `recentDatapoints`, `[valor]`; `evaluatedDatapoints` (si la plantilla lo
    trae), un único punto de ese periodo con el `sampleCount` de la plantilla (o 1.0); `statistic`,
    `period` y `threshold` salen de la alarma. El resto se deja como está.

    `ValueError` si a la alarma le falta `Statistic`, `Period` o `Threshold`, si `ahora` no tiene
    zona, si `valor` no supera el umbral o si el JSON pasa de 4000 caracteres."""
    faltan = [c for c in ("Statistic", "Period", "Threshold") if alarma.get(c) is None]
    if faltan:
        raise ValueError(f"a la alarma {alarma.get('AlarmName')!r} le faltan {faltan}")
    if ahora.tzinfo is None:
        raise ValueError("`ahora` necesita zona horaria")
    periodo, umbral = int(alarma["Period"]), float(alarma["Threshold"])
    if not valor > umbral:
        raise ValueError(f"el valor {valor} no supera el umbral {umbral}")
    segundos = int(ahora.timestamp())
    inicio = _fecha_aws(datetime.fromtimestamp(segundos - segundos % periodo - periodo, UTC))
    nuevos = {
        "querydate": _fecha_aws(ahora),
        "startdate": inicio,
        "statistic": alarma["Statistic"],
        "period": periodo,
        "recentdatapoints": [valor],
        "threshold": umbral,
    }
    plantilla = plantilla_de(alarma)
    datos: dict[str, Any] = {}
    for clave, actual in (FORMATO_POR_OMISION if plantilla is None else plantilla).items():
        if clave.lower() == "evaluateddatapoints":
            primero = actual[0] if isinstance(actual, list) and actual else None
            datos[clave] = [_punto(primero, inicio, valor)]
        else:
            datos[clave] = nuevos.get(clave.lower(), actual)
    texto = json.dumps(datos, separators=(",", ":"))
    if len(texto) > MAX_DATOS_DE_ESTADO:
        raise ValueError(f"StateReasonData de {len(texto)} caracteres (máximo 4000)")
    return texto


# --- Guardas y AWS (solo lecturas, salvo las publicaciones y `set_alarm_state`) ----------------


def _requerido(motivo: str) -> NoReturn:
    """Falta de configuración o precondición: falla con E2E_REQUERIDO=1, si no se omite."""
    if os.getenv("E2E_REQUERIDO") == "1":
        pytest.fail(motivo)
    pytest.skip(motivo)


def _serializable(valor: Any) -> Any:
    if isinstance(valor, datetime):
        return _iso_z(valor)
    if isinstance(valor, set):
        return sorted(valor)
    if is_dataclass(valor) and not isinstance(valor, type):
        return asdict(valor)
    return str(valor)


class Salida:
    """Lo que la fase deja en el log y en $GITHUB_STEP_SUMMARY. Las fases no leen secretos: de las
    colas se imprime el nombre y de las tareas, su id."""

    def __init__(self, fase: str) -> None:
        self.fase = fase
        self._con_titulo = False

    def _resumen(self, texto: str) -> None:
        archivo = os.getenv("GITHUB_STEP_SUMMARY")
        if not archivo:
            return
        with open(archivo, "a", encoding="utf-8") as salida:
            if not self._con_titulo:
                salida.write(f"\n### {HISTORIA} · {self.fase}\n\n")
                self._con_titulo = True
            salida.write(f"{limitar(texto)}\n")

    def linea(self, etiqueta: str, texto: str) -> None:
        print(f"{HISTORIA} {self.fase}, {etiqueta}: {texto}", flush=True)
        self._resumen(f"- {etiqueta}: {texto}")

    def tabla(
        self, titulo: str, encabezados: Sequence[str], filas: Iterable[Iterable[Any]]
    ) -> None:
        self._resumen(f"\n**{titulo}**\n\n{tabla(encabezados, filas)}\n")

    def json(self, prefijo: str, datos: dict[str, Any]) -> None:
        texto = json.dumps(datos, ensure_ascii=False, separators=(",", ":"), default=_serializable)
        print(f"{prefijo} {texto}", flush=True)


@dataclass(frozen=True)
class Aws:
    autoscaling: Any
    cloudwatch: Any
    ecs: Any
    logs: Any
    s3: Any
    sns: Any
    sqs: Any


@dataclass(frozen=True)
class Operacion:
    fase: str
    contexto: Contexto
    aws: Aws
    salida: Salida
    recurso: str
    minimo: int
    maximo: int
    backlog: Politica
    antiguedad: dict[str, Politica]  # cola → su política -antiguedad-<cola>
    servicio: dict[str, Any]  # DescribeServices al empezar


def _clientes_aws(region: str) -> Aws:
    try:
        import boto3
    except ImportError:  # pragma: no cover
        _requerido(f"N2 {HISTORIA}: boto3 no está instalado")
    try:
        sesion = boto3.session.Session(region_name=region)
        sesion.client("sts").get_caller_identity()
    except (BotoCoreError, ClientError) as error:
        _requerido(f"N2 {HISTORIA}: sin acceso a AWS ({type(error).__name__})")
    cliente = sesion.client
    return Aws(
        autoscaling=cliente("application-autoscaling"),
        cloudwatch=cliente("cloudwatch"),
        ecs=cliente("ecs"),
        logs=cliente("logs"),
        s3=cliente("s3"),
        sns=cliente("sns"),
        sqs=cliente("sqs"),
    )


def _paginar(
    llamada: Callable[..., dict[str, Any]],
    clave: str,
    token: str = "NextToken",
    paginas: int = MAX_PAGINAS,
    **parametros: Any,
) -> list[Any]:
    elementos: list[Any] = []
    for _ in range(paginas):
        respuesta = llamada(**parametros)
        elementos.extend(respuesta.get(clave) or [])
        siguiente = respuesta.get(token)
        if not siguiente:
            break
        parametros[token] = siguiente
    return elementos


def _codigo(error: Exception) -> str:
    respuesta = getattr(error, "response", None) or {}
    return str((respuesta.get("Error") or {}).get("Code") or type(error).__name__)


def _servicio(aws: Aws, contexto: Contexto) -> dict[str, Any]:
    servicios = aws.ecs.describe_services(cluster=contexto.cluster, services=[SERVICIO])["services"]
    if len(servicios) != 1:
        pytest.fail(f"{HISTORIA}: {len(servicios)} servicios {SERVICIO} en {contexto.cluster}")
    return dict(servicios[0])


def _atributos(aws: Aws, url: str) -> tuple[int, int]:
    """(visibles, no visibles) de la cola, de `GetQueueAttributes`."""
    atributos = aws.sqs.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return (
        int(atributos.get("ApproximateNumberOfMessages", 0)),
        int(atributos.get("ApproximateNumberOfMessagesNotVisible", 0)),
    )


def _visibles(aws: Aws, contexto: Contexto, colas: Iterable[str] = COLAS) -> dict[str, int]:
    return {cola: _atributos(aws, contexto.colas[cola]["url"])[0] for cola in colas}


def _dlq(aws: Aws, contexto: Contexto) -> dict[str, int]:
    """Los visibles de las dos DLQ (`<prefijo>-<cola>-dlq`, infra/platform/messaging.tf:13)."""
    return {
        cola: _atributos(
            aws, aws.sqs.get_queue_url(QueueName=contexto.colas[cola]["dlq_name"])["QueueUrl"]
        )[0]
        for cola in COLAS_DLQ
    }


def _consulta(
    ident: str, espacio: str, metrica: str, dimensiones: dict[str, str], estadistica: str
) -> dict[str, Any]:
    return {
        "Id": ident,
        "MetricStat": {
            "Metric": {
                "Namespace": espacio,
                "MetricName": metrica,
                "Dimensions": [{"Name": k, "Value": v} for k, v in dimensiones.items()],
            },
            "Period": 60,
            "Stat": estadistica,
        },
        "ReturnData": True,
    }


def _consulta_rtc(contexto: Contexto) -> dict[str, Any]:
    """RunningTaskCount de Container Insights con `Average`, como la política (plan §b-1)."""
    return _consulta(
        "rtc",
        "ECS/ContainerInsights",
        "RunningTaskCount",
        {"ClusterName": contexto.cluster, "ServiceName": SERVICIO},
        "Average",
    )


def _consultas_de_linea(contexto: Contexto) -> list[dict[str, Any]]:
    consultas = [_consulta_rtc(contexto)]
    for cola in COLAS:
        nombre, corto = contexto.colas[cola]["name"], CORTO[cola]
        for clave, metrica, estadistica in (
            ("visibles", "ApproximateNumberOfMessagesVisible", "Average"),
            ("no_visibles", "ApproximateNumberOfMessagesNotVisible", "Average"),
            ("antiguedad", "ApproximateAgeOfOldestMessage", "Maximum"),
            ("enviados", "NumberOfMessagesSent", "Sum"),
            ("borrados", "NumberOfMessagesDeleted", "Sum"),
        ):
            consultas.append(
                _consulta(
                    f"{clave}_{corto}", "AWS/SQS", metrica, {"QueueName": nombre}, estadistica
                )
            )
    for cola in COLAS_DLQ:
        consultas.append(
            _consulta(
                f"dlq_{CORTO[cola]}",
                "AWS/SQS",
                "ApproximateNumberOfMessagesVisible",
                {"QueueName": contexto.colas[cola]["dlq_name"]},
                "Maximum",
            )
        )
    return consultas


def _metricas(
    aws: Aws, consultas: list[dict[str, Any]], desde: datetime, hasta: datetime
) -> dict[str, dict[datetime, float]]:
    """Por id de consulta, {minuto: valor} de GetMetricData."""
    series: dict[str, dict[datetime, float]] = {c["Id"]: {} for c in consultas}
    for resultado in _paginar(
        aws.cloudwatch.get_metric_data,
        "MetricDataResults",
        MetricDataQueries=consultas,
        StartTime=desde,
        EndTime=hasta,
        ScanBy="TimestampAscending",
    ):
        serie = series.setdefault(resultado["Id"], {})
        for momento, valor in zip(resultado.get("Timestamps", []), resultado.get("Values", [])):
            serie[_minuto_abajo(momento)] = float(valor)
    return series


def _dormir_hasta(momento: datetime) -> None:
    falta = (momento - datetime.now(UTC)).total_seconds()
    if falta > 0:
        time.sleep(falta)


def _publicar(aws: Aws, contexto: Contexto, sobres: list[dict[str, Any]]) -> tuple[int, dict]:
    """`PublishBatch` de `LOTE` en `LOTE`, con `HILOS` en paralelo; los fallidos se reintentan una
    vez. Devuelve los publicados y los códigos de error de los que fallaron."""

    def enviar(lote: list[dict[str, Any]]) -> tuple[int, list[str]]:
        entradas = [
            {
                "Id": str(i),
                "Message": json.dumps(sobre, separators=(",", ":")),
                "MessageAttributes": {
                    "eventType": {"DataType": "String", "StringValue": sobre["type"]}
                },
            }
            for i, sobre in enumerate(lote)
        ]
        fallidos = (
            aws.sns.publish_batch(
                TopicArn=contexto.topico, PublishBatchRequestEntries=entradas
            ).get("Failed")
            or []
        )
        if fallidos:
            reintento = [entradas[int(f["Id"])] for f in fallidos]
            fallidos = (
                aws.sns.publish_batch(
                    TopicArn=contexto.topico, PublishBatchRequestEntries=reintento
                ).get("Failed")
                or []
            )
        return len(lote) - len(fallidos), [str(f.get("Code")) for f in fallidos]

    with ThreadPoolExecutor(HILOS) as grupo:
        resultados = list(grupo.map(enviar, lotes(sobres)))
    codigos = Counter(c for _, lista in resultados for c in lista)
    return sum(n for n, _ in resultados), dict(codigos)


class Auditoria:
    """Los registros de los lotes `cadena/<secuencia>-<hash>.json` del bucket de auditoría
    (services/auditoria/.../almacen_s3.py:3). Cada lectura lista el prefijo entero y lee, en
    paralelo, las claves escritas desde `desde` que todavía no leyó. No usa `StartAfter`: si el
    bucket sobrevive a un ciclo de INT (Object Lock de 30 días), la secuencia de una cadena nueva
    puede quedar por debajo de las claves viejas."""

    def __init__(self, aws: Aws, bucket: str) -> None:
        self._s3 = aws.s3
        self._bucket = bucket
        self._leidas: set[str] = set()
        self._registros: list[dict[str, Any]] = []

    def registros(self, desde: datetime) -> list[dict[str, Any]]:
        claves = []
        paginas = self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix="cadena/"
        )
        for pagina in paginas:
            for objeto in pagina.get("Contents", []):
                if objeto["LastModified"] >= desde and objeto["Key"] not in self._leidas:
                    claves.append(objeto["Key"])
        self._leidas.update(claves)

        def leer(clave: str) -> list[dict[str, Any]]:
            cuerpo = self._s3.get_object(Bucket=self._bucket, Key=clave)["Body"].read()
            return list(json.loads(cuerpo).get("registros") or [])

        with ThreadPoolExecutor(HILOS) as grupo:
            for registros in grupo.map(leer, claves):
                self._registros.extend(registros)
        return self._registros


def _evaluados(registros: Iterable[dict[str, Any]], ids: set[str]) -> dict[str, list[dict]]:
    """Los registros `EventoParametricoEvaluado` de `ids`, por `resourceId` (= eventoId)."""
    encontrados: dict[str, list[dict[str, Any]]] = {}
    for registro in registros:
        if registro.get("accion") == EVALUADO and registro.get("resourceId") in ids:
            encontrados.setdefault(str(registro["resourceId"]), []).append(registro)
    return encontrados


def _decision(registro: dict[str, Any]) -> str | None:
    evento = registro.get("evento")
    try:
        sobre: Any = json.loads(evento) if isinstance(evento, str) else evento
        return str(sobre["payload"]["decision"])
    except (ValueError, TypeError, KeyError):
        return None


def _politicas(op_respuesta: dict[str, Any], prefijo: str) -> tuple[Politica, dict[str, Politica]]:
    nombre = f"{prefijo}-{SERVICIO}-backlog"
    try:
        backlog = politica_de(op_respuesta, nombre)
    except LookupError:
        _requerido(f"N2 {HISTORIA}: no existe la política {nombre}")
    except ValueError as error:
        pytest.fail(f"N2 {HISTORIA}: no se reconocen las alarmas de {nombre}: {error}")
    if not backlog.por_tarea:
        _requerido(
            f"N2 {HISTORIA}: {nombre} no tiene RunningTaskCount en su configuración "
            f"({list(backlog.metricas)}): ¿está aplicada la 25.4 en INT?"
        )
    antiguedad: dict[str, Politica] = {}
    for cola in COLAS:
        nombre_cola = f"{prefijo}-{SERVICIO}-antiguedad-{cola}"
        try:
            antiguedad[cola] = politica_de(op_respuesta, nombre_cola, exige_baja=False)
        except LookupError:
            _requerido(f"N2 {HISTORIA}: no existe la política {nombre_cola}")
        except ValueError as error:
            pytest.fail(f"N2 {HISTORIA}: no se reconocen las alarmas de {nombre_cola}: {error}")
    return backlog, antiguedad


def _abrir(request: pytest.FixtureRequest, fase: str, desde_el_minimo: bool) -> Operacion:
    """Las guardas, en orden, antes de publicar o forzar nada."""
    if not seleccionada(request.config.getoption("keyword"), fase):
        pytest.skip(f"{HISTORIA}: {MOTIVOS[fase]}; selecciónala con -k {fase}")
    if os.getenv("ENV") != AMBIENTE:
        pytest.skip(f"{HISTORIA}: {fase} solo corre con ENV=int (ENV={os.getenv('ENV')!r})")
    try:
        contexto = contexto_de(request.getfixturevalue("ambiente").salidas)
    except ValueError as error:
        _requerido(f"N2 {HISTORIA}: {error}")
    aws = _clientes_aws(contexto.region)
    salida = Salida(fase)
    recurso = f"service/{contexto.cluster}/{SERVICIO}"
    try:
        objetivos = aws.autoscaling.describe_scalable_targets(
            ServiceNamespace="ecs", ResourceIds=[recurso], ScalableDimension=DIMENSION
        )["ScalableTargets"]
        respuesta = {
            "ScalingPolicies": _paginar(
                aws.autoscaling.describe_scaling_policies,
                "ScalingPolicies",
                ServiceNamespace="ecs",
                ResourceId=recurso,
                ScalableDimension=DIMENSION,
            )
        }
        servicio = _servicio(aws, contexto)
    except (BotoCoreError, ClientError) as error:
        _requerido(f"N2 {HISTORIA}: no se pudo leer {recurso} ({_codigo(error)})")
    if len(objetivos) != 1:
        _requerido(f"N2 {HISTORIA}: {recurso} no tiene scalable target ({len(objetivos)})")
    minimo, maximo = int(objetivos[0]["MinCapacity"]), int(objetivos[0]["MaxCapacity"])
    deseadas, corriendo = int(servicio["desiredCount"]), int(servicio["runningCount"])
    # Va al resumen antes de las guardas: el usuario no tiene la consola de ECS (plan §f, paso 5).
    salida.linea(
        "servicio al empezar",
        f"{SERVICIO} con desiredCount {deseadas}, runningCount {corriendo} y pendingCount "
        f"{servicio.get('pendingCount')} (min {minimo}, max {maximo})",
    )
    if not minimo < maximo:
        _requerido(
            f"N2 {HISTORIA}: {recurso} con min = {minimo} y max = {maximo}; hace falta min < max"
        )
    if (objetivos[0].get("SuspendedState") or {}).get("DynamicScalingOutSuspended"):
        _requerido(f"N2 {HISTORIA}: el escalado hacia fuera de {recurso} está suspendido")
    backlog, antiguedad = _politicas(respuesta, contexto.prefijo)
    if desde_el_minimo and not deseadas == minimo == corriendo:
        _requerido(
            f"N2 {HISTORIA}: {SERVICIO} con desiredCount = {deseadas} y runningCount = {corriendo}; "
            f"hace falta partir de {minimo} y {minimo} (¿un escalado o un despliegue anterior aún no "
            "termina? Espera 5 min y vuelve a lanzarla)"
        )
    salida.linea(
        "políticas",
        f"{backlog.nombre}: T = {backlog.objetivo:g} visibles por tarea, enfriamientos "
        f"{backlog.enfriamiento_salida} s y {backlog.enfriamiento_entrada} s, métricas "
        f"{list(backlog.metricas)}; "
        + "; ".join(
            f"{p.nombre}: {p.objetivo:g} s, solo de salida {p.desactiva_entrada}"
            for p in antiguedad.values()
        ),
    )
    return Operacion(
        fase, contexto, aws, salida, recurso, minimo, maximo, backlog, antiguedad, servicio
    )


def _alarma(aws: Aws, nombre: str) -> dict[str, Any]:
    alarmas = aws.cloudwatch.describe_alarms(AlarmNames=[nombre]).get("MetricAlarms", [])
    if len(alarmas) != 1:
        pytest.fail(f"{len(alarmas)} alarmas {nombre!r} en DescribeAlarms")
    return dict(alarmas[0])


def _historial(aws: Aws, alarma: str, desde: datetime, hasta: datetime) -> list[dict[str, Any]]:
    return _paginar(
        aws.cloudwatch.describe_alarm_history,
        "AlarmHistoryItems",
        AlarmName=alarma,
        HistoryItemType="StateUpdate",
        StartDate=desde,
        EndDate=hasta,
        ScanBy="TimestampAscending",
        MaxRecords=100,
    )


def _actividades(op: Operacion, desde: datetime) -> list[Actividad]:
    items = _paginar(
        op.aws.autoscaling.describe_scaling_activities,
        "ScalingActivities",
        paginas=10,
        ServiceNamespace="ecs",
        ResourceId=op.recurso,
        ScalableDimension=DIMENSION,
        MaxResults=50,
    )
    return [a for a in actividades_de(items) if a.inicio >= desde]


def _hora(valor: datetime | None) -> str:
    return instante(valor).isoformat(timespec="seconds") if valor else "—"


# --- Las fases --------------------------------------------------------------------------------


def test_hu_w25_publicar(request: pytest.FixtureRequest) -> None:
    inicio_prueba = time.monotonic()
    op = _abrir(request, "hu_w25_publicar", desde_el_minimo=True)
    aws, contexto, salida = op.aws, op.contexto, op.salida
    objetivo = op.backlog.objetivo
    eventos_url = contexto.colas[COLA_RAFAGA]["url"]

    ahora = datetime.now(UTC)
    rtc = _metricas(aws, [_consulta_rtc(contexto)], ahora - timedelta(minutes=5), ahora)["rtc"]
    if not rtc:
        _requerido(
            f"N2 {HISTORIA}: RunningTaskCount de {SERVICIO} sin datos en los últimos 5 min: ¿está "
            "Container Insights encendido en INT (25.1, Infra - Desplegar con plataforma)?"
        )
    visibles = _visibles(aws, contexto)
    por_tarea = sum(visibles.values()) / max(int(op.servicio["runningCount"]), 1)
    salida.linea(
        "colas al empezar",
        f"visibles {visibles}, {por_tarea:g} por tarea (T = {objetivo:g}); RunningTaskCount "
        f"{[round(v, 2) for _, v in sorted(rtc.items())]} en los últimos 5 min",
    )
    if not por_tarea < objetivo:
        _requerido(f"N2 {HISTORIA}: las colas ya tienen {por_tarea:g} visibles por tarea, no < T")
    dlq_al_empezar = _dlq(aws, contexto)
    salida.linea("DLQ al empezar", str(dlq_al_empezar))

    sufijo = nuevo_sufijo(datetime.now(UTC))
    salida.linea(
        "corrida",
        f"sufijo {sufijo}, eventoOrigen E2E-W25-{sufijo}, fuente {FUENTE}, zona {ZONA} (sin "
        "coberturas: SIN_POLIZAS, sin siniestros)",
    )
    auditoria = Auditoria(aws, contexto.bucket)

    # El centinela: la ruta SNS → SQS → pagos-parametricos → auditoría funciona (hallazgo 3).
    centinela = id_externo(sufijo, "S", 1, 0)
    centinela_id = evento_id_de(centinela)
    enviado = datetime.now(UTC)
    publicados, fallos = _publicar(aws, contexto, [sobre_observado(centinela, sufijo, enviado)])
    if publicados != 1:
        pytest.fail(f"{HISTORIA}: no se pudo publicar el centinela ({fallos})")
    limite = time.monotonic() + ESPERA_CENTINELA_S
    evaluado: list[dict[str, Any]] = []
    while not evaluado and time.monotonic() < limite:
        time.sleep(SONDEO_S)
        registros = auditoria.registros(enviado - timedelta(minutes=1))
        evaluado = _evaluados(registros, {centinela_id}).get(centinela_id, [])
    if not evaluado:
        salida.linea(
            "centinela", f"no se vio evaluado en {ESPERA_CENTINELA_S} s; no se publica nada"
        )
        pytest.fail(
            f"{HISTORIA}: el centinela {centinela} no se vio evaluado en la auditoría en "
            f"{ESPERA_CENTINELA_S} s; no se publicó la ráfaga. Espera 10 min y vuelve a lanzarla "
            "(hallazgo 3, docs/hallazgos-hu-w07-w08.md)"
        )
    decision = _decision(evaluado[0])
    segundos = round((datetime.now(UTC) - enviado).total_seconds())
    salida.linea(
        "centinela", f"evaluado ({decision}) en unos {segundos} s; eventoId {centinela_id}"
    )
    if decision != DECISION_ESPERADA:
        pytest.fail(
            f"{HISTORIA}: el centinela se evaluó {decision}, no {DECISION_ESPERADA}: la zona {ZONA} "
            "tiene coberturas y la ráfaga crearía siniestros. No se publicó la ráfaga"
        )

    # La ráfaga, al inicio del minuto siguiente: su primer minuto es completo (plan §c.4, punto 1).
    t0 = _minuto_arriba(datetime.now(UTC) + timedelta(seconds=1))
    valores = [id_externo(sufijo, "R", RAFAGA, n) for n in range(RAFAGA)]
    _dormir_hasta(t0)
    inicio = datetime.now(UTC)
    publicados, fallos = _publicar(
        aws, contexto, [sobre_observado(v, sufijo, inicio) for v in valores]
    )
    duracion = (datetime.now(UTC) - inicio).total_seconds()
    salida.linea(
        "ráfaga",
        f"{publicados} de {RAFAGA} publicados (PublishBatch de {LOTE}) desde las {_hora(inicio)} en "
        f"{duracion:.1f} s" + (f"; fallaron {fallos}" if fallos else ""),
    )
    if publicados != RAFAGA:
        pytest.fail(f"{HISTORIA}: la ráfaga publicó {publicados} de {RAFAGA} ({fallos})")

    # El ritmo por tarea del primer minuto (plan §c.4, punto 2).
    cierre = t0 + timedelta(minutes=1)
    _dormir_hasta(cierre + timedelta(seconds=2))
    visibles_al_cerrar = _atributos(aws, eventos_url)[0]
    tareas = int(_servicio(aws, contexto)["runningCount"])
    if tareas < 1:
        pytest.fail(f"{HISTORIA}: {SERVICIO} sin tareas en marcha al cerrar el primer minuto")
    borrados = None
    consulta = _consulta(
        "borrados",
        "AWS/SQS",
        "NumberOfMessagesDeleted",
        {"QueueName": contexto.colas[COLA_RAFAGA]["name"]},
        "Sum",
    )
    limite = time.monotonic() + ESPERA_PUNTO_S
    while borrados is None and time.monotonic() < limite:
        borrados = _metricas(aws, [consulta], t0, cierre)["borrados"].get(t0)
        if borrados is None:
            time.sleep(15)
    caida = None
    if borrados is None:
        desde_rafaga = (datetime.now(UTC) - inicio).total_seconds()
        antes, marca = _atributos(aws, eventos_url)[0], time.monotonic()
        time.sleep(SEPARACION_CAIDA_S)
        despues = _atributos(aws, eventos_url)[0]
        caida = Caida(antes, despues, round(time.monotonic() - marca, 1), round(desde_rafaga, 1))
    ritmo = ritmo_por_tarea(tareas, borrados, visibles_al_cerrar, caida)
    salida.linea(
        "ritmo por tarea",
        f"{ritmo.valor:.2f} mensajes/s por tarea; fuente {ritmo.fuente}"
        + (f" (el punto no llegó en {ESPERA_PUNTO_S} s)" if borrados is None else "")
        + ("; cota inferior: la cola llegó a 0" if ritmo.cota_inferior else "")
        + f"; {ritmo.calculo}; visibles al cerrar el minuto {visibles_al_cerrar}",
    )

    # El complemento, enseguida (plan §c.4, puntos 3 a 5).
    visibles_ahora = _atributos(aws, eventos_url)[0]
    calculo = complemento(ritmo.valor, objetivo, visibles_ahora)
    publicados_c, fallos_c = 0, dict[str, int]()
    if calculo.eventos:
        momento = datetime.now(UTC)
        sobres = [
            sobre_observado(id_externo(sufijo, "C", calculo.eventos, n), sufijo, momento)
            for n in range(calculo.eventos)
        ]
        publicados_c, fallos_c = _publicar(aws, contexto, sobres)
    salida.linea(
        "complemento",
        f"{publicados_c} eventos publicados ({calculo.motivo}); c {ritmo.valor:.3f}, T "
        f"{objetivo:g}, ventana {VENTANA_S} s, margen {MARGEN}, visibles_ahora {visibles_ahora}; "
        f"tope {'aplicado' if calculo.tope_aplicado else 'no aplicado'}; {calculo.calculo}"
        + (f"; fallaron {fallos_c}" if fallos_c else ""),
    )
    salida.linea(
        "total publicado",
        f"{RAFAGA + publicados_c} (ráfaga {RAFAGA} + complemento {publicados_c}; tope "
        f"{TOPE_TOTAL}; sin contar el centinela ni el goteo)",
    )

    # La subida, hasta TOPE_PUBLICAR_S desde el inicio de la prueba (cabe en los 20 min del job).
    muestras: list[tuple[datetime, int, int, int]] = []
    subida = bajo_desde = None
    while True:
        servicio = _servicio(aws, contexto)
        ahora = datetime.now(UTC)
        total = sum(_visibles(aws, contexto).values())
        deseadas, corriendo = int(servicio["desiredCount"]), int(servicio["runningCount"])
        muestras.append((ahora, deseadas, corriendo, total))
        if bajo_desde is None and corriendo and total / corriendo < UMBRAL_REDUCCION * objetivo:
            bajo_desde = ahora
        if deseadas > op.minimo:
            subida = (ahora, deseadas)
            break
        if time.monotonic() - inicio_prueba >= TOPE_PUBLICAR_S:
            break
        time.sleep(MUESTREO_S)
    salida.linea(
        "subida",
        f"desiredCount {op.minimo} → {subida[1]} a las {_hora(subida[0])} "
        f"({(subida[0] - inicio).total_seconds():.0f} s desde la ráfaga)"
        if subida
        else f"no se vio en el tope de la prueba ({TOPE_PUBLICAR_S // 60} min); hu_w25_recoger "
        "la reconstruye con las actividades de escalado",
    )
    ahora = datetime.now(UTC)
    hora = hora_para_recoger(ahora, bajo_desde, None, int(op.backlog.enfriamiento_entrada or 300))
    salida.linea(
        "recoger desde",
        f"{_hora(ahora)}: lanza hu_w25_recoger enseguida (el goteo debe coincidir con la reducción, "
        f"que se espera hacia las {_hora(hora)})",
    )
    salida.json(
        PREFIJO_PUBLICACION,
        {
            "sufijo": sufijo,
            "t0": inicio,
            "dlqAlEmpezar": dlq_al_empezar,
            "centinela": {"eventoId": centinela_id, "decision": decision},
            "rafaga": {"publicados": publicados, "duracionS": duracion},
            "ritmo": ritmo,
            "visiblesAlCerrar": visibles_al_cerrar,
            "caida": caida,
            "complemento": calculo,
            "complementoPublicados": publicados_c,
            "muestras": muestras,
            "subida": subida,
            "recogerDesde": ahora,
            "reduccionHacia": hora,
        },
    )
    if publicados_c != calculo.eventos:
        pytest.fail(f"{HISTORIA}: el complemento publicó {publicados_c} de {calculo.eventos}")


def _eventos_ecs(op: Operacion, desde: datetime, hasta: datetime) -> list[dict[str, Any]]:
    """Los eventos `ECS Task State Change` de pagos-parametricos del events_log_group, más las
    tareas que ECS todavía lista (en marcha y detenidas), con la misma forma."""
    aws, contexto = op.aws, op.contexto
    eventos: list[dict[str, Any]] = []
    for evento in _paginar(
        aws.logs.filter_log_events,
        "events",
        token="nextToken",
        logGroupName=contexto.eventos_ecs,
        startTime=int(desde.timestamp() * 1000),
        endTime=int(hasta.timestamp() * 1000),
        filterPattern=f'{{ $.detail.group = "service:{SERVICIO}" }}',
    ):
        try:
            eventos.append(json.loads(evento.get("message", "")))
        except ValueError:
            continue
    for estado in ("RUNNING", "STOPPED"):
        arns = aws.ecs.list_tasks(
            cluster=contexto.cluster, serviceName=SERVICIO, desiredStatus=estado
        )["taskArns"]
        for grupo in lotes(arns, 100):
            for tarea in aws.ecs.describe_tasks(cluster=contexto.cluster, tasks=grupo)["tasks"]:
                detalle = {
                    clave: tarea.get(clave)
                    for clave in (
                        "taskArn",
                        "group",
                        "lastStatus",
                        "startedAt",
                        "stoppingAt",
                        "stoppedAt",
                        "stoppedReason",
                        "stopCode",
                    )
                    if tarea.get(clave) is not None
                }
                detalle["containers"] = [
                    {"name": c.get("name"), "exitCode": c.get("exitCode")}
                    for c in tarea.get("containers", [])
                ]
                eventos.append({"detail-type": "ECS Task State Change", "detail": detalle})
    return eventos


def _liberados(op: Operacion, desde: datetime, hasta: datetime) -> dict[str, Any]:
    """Los `inbox: mensaje liberado por la parada` de la ventana del retiro (Logs Insights)."""
    grupo = f"/ecs/{op.contexto.prefijo}/{SERVICIO}"  # infra/apps/ecs.tf:3
    consulta = (
        f'fields @timestamp, @logStream | filter @message like "{MENSAJE_LIBERADO}" '
        "| sort @timestamp asc | limit 1000"
    )
    try:
        consulta_id = op.aws.logs.start_query(
            logGroupName=grupo,
            startTime=int(desde.timestamp()),
            endTime=int(hasta.timestamp()) + 1,
            queryString=consulta,
            limit=1000,
        )["queryId"]
        limite = time.monotonic() + ESPERA_CONSULTA_S
        while True:
            respuesta = op.aws.logs.get_query_results(queryId=consulta_id)
            if respuesta.get("status") in ("Complete", "Failed", "Cancelled", "Timeout"):
                break
            if time.monotonic() > limite:
                return {"estado": f"sin terminar en {ESPERA_CONSULTA_S} s", "liberados": None}
            time.sleep(2)
    except (BotoCoreError, ClientError) as error:
        return {"estado": f"no se pudo consultar ({_codigo(error)})", "liberados": None}
    filas = [{c["field"]: c["value"] for c in fila} for fila in respuesta.get("results", [])]
    return {
        "estado": respuesta.get("status"),
        "grupo": grupo,
        "desde": desde,
        "hasta": hasta,
        "liberados": len(filas),
        "porFlujo": dict(Counter(f.get("@logStream", "") for f in filas)),
    }


def test_hu_w25_recoger(request: pytest.FixtureRequest) -> None:
    inicio_prueba = time.monotonic()
    op = _abrir(request, "hu_w25_recoger", desde_el_minimo=False)
    aws, contexto, salida = op.aws, op.contexto, op.salida
    avisos: list[str] = []

    # El goteo, mientras desiredCount > mínimo (plan §b-11).
    muestras: list[tuple[datetime, int, int]] = [
        (datetime.now(UTC), int(op.servicio["desiredCount"]), int(op.servicio["runningCount"]))
    ]
    goteo: list[str] = []
    sufijo_goteo = None
    if muestras[0][1] > op.minimo:
        sufijo_goteo = nuevo_sufijo(datetime.now(UTC))
        empezo, fallos = datetime.now(UTC), Counter[str]()
        limite = time.monotonic() + GOTEO_TOPE_S
        proximo = time.monotonic() + MUESTREO_S
        while time.monotonic() < limite and len(goteo) < GOTEO_MAXIMO:
            marca = time.monotonic()
            valor = id_externo(sufijo_goteo, "G", None, len(goteo))
            sobre = sobre_observado(valor, sufijo_goteo, datetime.now(UTC))
            try:
                aws.sns.publish(
                    TopicArn=contexto.topico,
                    Message=json.dumps(sobre, separators=(",", ":")),
                    MessageAttributes={
                        "eventType": {"DataType": "String", "StringValue": OBSERVADO}
                    },
                )
                goteo.append(valor)
            except (BotoCoreError, ClientError) as error:
                fallos[_codigo(error)] += 1
            if time.monotonic() >= proximo:
                proximo += MUESTREO_S
                servicio = _servicio(aws, contexto)
                muestra = (
                    datetime.now(UTC),
                    int(servicio["desiredCount"]),
                    int(servicio["runningCount"]),
                )
                muestras.append(muestra)
                if muestra[1] <= op.minimo:
                    break
            time.sleep(max(0.0, GOTEO_INTERVALO_S - (time.monotonic() - marca)))
        salida.linea(
            "goteo",
            f"{len(goteo)} eventos a 1/s desde las {_hora(empezo)} hasta las "
            f"{_hora(datetime.now(UTC))} (eventoOrigen E2E-W25-{sufijo_goteo}), aparte de la ráfaga "
            f"y del complemento; tope {GOTEO_TOPE_S // 60} min y {GOTEO_MAXIMO} eventos"
            + (f"; fallaron {dict(fallos)}" if fallos else ""),
        )
    else:
        salida.linea(
            "goteo", f"sin goteo: {SERVICIO} ya estaba en el mínimo ({op.minimo}); solo recoge"
        )
    final = _servicio(aws, contexto)
    muestras.append((datetime.now(UTC), int(final["desiredCount"]), int(final["runningCount"])))
    volvio = int(final["desiredCount"]) == op.minimo

    # La corrida de publicar y lo publicado, desde la auditoría.
    auditoria = Auditoria(aws, contexto.bucket)
    desde_auditoria = datetime.now(UTC) - VENTANA_AUDITORIA
    registros = auditoria.registros(desde_auditoria)
    corridas = corridas_de(registros)
    if sufijo_goteo:
        corridas.pop(sufijo_goteo, None)  # el goteo de esta corrida se reconcilia entero
    try:
        publicar, goteos, esperados = esperados_de(corridas)
    except LookupError as error:
        pytest.fail(
            f"{HISTORIA}: {error} en la auditoría de las últimas "
            f"{VENTANA_AUDITORIA.total_seconds() / 3600:g} h: ¿corrió hu_w25_publicar?"
        )
    esperados += [evento_id_de(v) for v in goteo]
    propios = set(esperados)
    limite = min(time.monotonic() + ESPERA_AUDITORIA_S, inicio_prueba + TOPE_RECOGER_S)
    while len(_evaluados(registros, propios)) < len(propios) and time.monotonic() < limite:
        time.sleep(SONDEO_S)
        registros = auditoria.registros(desde_auditoria)

    t0 = publicar.primeras.get("R") or instante(
        datetime.strptime(publicar.sufijo[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    )
    desde = _minuto_abajo(t0 - ANTES_DE_LA_RAFAGA)
    hasta = _minuto_arriba(datetime.now(UTC))

    # La línea de tiempo por minuto.
    series = _metricas(aws, _consultas_de_linea(contexto), desde, hasta)
    eventos = _eventos_ecs(op, desde - VENTANA_AUDITORIA, hasta)
    series["tareas_eventos"] = {
        m: float(n) for m, n in tareas_de_eventos(eventos, desde, hasta).items()
    }
    actividades = _actividades(op, desde)
    capacidad = cambios_de_capacidad(actividades, op.minimo)
    alarmas = {"backlog_alta": op.backlog.alarma_alta}
    if op.backlog.alarma_baja:
        alarmas["backlog_baja"] = op.backlog.alarma_baja
    for cola, politica in op.antiguedad.items():
        alarmas[f"antiguedad_{CORTO[cola]}_alta"] = politica.alarma_alta
    historias = {
        clave: cambios_de_alarma(_historial(aws, nombre, desde, hasta))
        for clave, nombre in alarmas.items()
    }
    cambios: dict[str, list[tuple[datetime, Any]]] = {
        # Antes de la primera actividad, el mínimo: publicar partió de él (su guarda).
        "deseadas": [(desde - timedelta(seconds=1), op.minimo)]
        + [(c["hora"], c["a"]) for c in capacidad],
        "deseadas_ds": [(m[0], m[1]) for m in muestras],
        "corriendo_ds": [(m[0], m[2]) for m in muestras],
        **historias,
    }
    filas = linea_de_tiempo(desde, hasta, series, cambios)
    coincidencia = coinciden_tareas(series["rtc"], series["tareas_eventos"])

    # Subida, retiro y liberados.
    subida = next((c for c in capacidad if c["sentido"] == "subida"), None)
    reduccion = next(
        (
            c
            for c in capacidad
            if c["sentido"] == "reducción" and subida and c["hora"] > subida["hora"]
        ),
        None,
    )
    detenidas = (
        tareas_detenidas(eventos, reduccion["hora"] - timedelta(minutes=1)) if reduccion else []
    )
    liberados: dict[str, Any] = {"estado": "sin reducción todavía", "liberados": None}
    if reduccion:
        desde_retiro, hasta_retiro = ventana_de_retiro(
            reduccion["hora"], [(t["parada"], t["detenida"]) for t in detenidas]
        )
        liberados = _liberados(op, desde_retiro, hasta_retiro)

    # La reconciliación por eventoId.
    dlq_ahora = _dlq(aws, contexto)
    dlq_antes: dict[str, int] = {}
    dlq_despues: dict[str, int] = {}
    for cola in COLAS_DLQ:
        serie = series.get(f"dlq_{CORTO[cola]}") or {}
        previos = [v for m, v in sorted(serie.items()) if m <= _minuto_abajo(t0)]
        if previos:
            dlq_antes[cola] = int(previos[-1])
        else:
            dlq_antes[cola] = int(min(serie.values(), default=dlq_ahora[cola]))
            avisos.append(
                f"DLQ {cola}: sin punto antes de la ráfaga; se toma el mínimo de la ventana"
            )
        dlq_despues[cola] = int(max([dlq_ahora[cola], *serie.values()]))
    observados = [str(r.get("resourceId")) for r in registros if r.get("accion") == OBSERVADO]
    evaluados = [str(r.get("resourceId")) for r in registros if r.get("accion") == EVALUADO]
    resultado = reconciliar(esperados, observados, evaluados, dlq_antes, dlq_despues)
    decisiones = Counter(
        _decision(r) for lista in _evaluados(registros, propios).values() for r in lista
    )
    if set(decisiones) - {DECISION_ESPERADA}:
        avisos.append(f"decisiones distintas de {DECISION_ESPERADA}: {dict(decisiones)}")

    # El resumen.
    salida.linea(
        "corrida",
        f"publicar {publicar.sufijo} (ráfaga desde las {_hora(t0)}; declarados "
        f"{publicar.declarados}); goteos {[g.sufijo for g in goteos] + ([sufijo_goteo] if goteo else [])}",
    )
    salida.linea(
        "subida",
        f"a las {_hora(subida['hora'])}, de {subida['de']} a {subida['a']} tareas, pedida por "
        f"{subida['politica']}"
        if subida
        else "ninguna actividad de subida en la ventana (plan §f, paso 7: respaldo)",
    )
    if reduccion:
        retiro = (
            "; ".join(
                f"tarea {t['id']}, {t['stoppedReason']}, exitCode {t['exitCode']}, "
                f"{t['duracion_s']} s de stoppingAt a stoppedAt"
                for t in detenidas
            )
            or "sin tareas detenidas en los eventos de ECS"
        )
        salida.linea(
            "retiro",
            f"reducción a las {_hora(reduccion['hora'])}, de {reduccion['de']} a {reduccion['a']}, "
            f"por {reduccion['politica']}; {retiro}",
        )
    else:
        salida.linea("retiro", "todavía no hubo reducción")
    salida.linea(
        "liberados",
        f"{liberados.get('liberados')} «{MENSAJE_LIBERADO}» ({liberados.get('estado')}); se "
        "informa, no se exige (lo exigen el módulo y el SIGTERM de Linux, 25.5)",
    )
    salida.linea(
        "tareas (H-3)",
        f"{'coinciden' if coincidencia.ok else 'NO coinciden'}: RunningTaskCount frente a los eventos "
        f"de ECS en {coincidencia.comparados} minutos, racha máxima de {coincidencia.racha_maxima} "
        f"min con más de {TOLERANCIA_TAREAS} de diferencia (máximo {MAX_MINUTOS_DISCREPANCIA}); "
        f"{coincidencia.sin_datos} minutos sin alguno de los dos datos",
    )
    salida.linea(
        "reconciliación",
        f"{resultado.publicados} publicados, {resultado.evaluados} evaluados; faltantes "
        f"{len(resultado.faltantes)}, repetidos {len(resultado.repetidos)}, delta de las DLQ "
        f"{resultado.delta_dlq}; sin su observado en la auditoría {len(resultado.sin_observado)}; "
        f"decisiones {dict(decisiones)}",
    )
    if not volvio:
        hora = hora_para_recoger(
            datetime.now(UTC),
            bajo_objetivo_desde(filas, op.backlog.objetivo),
            reduccion["hora"] if reduccion else None,
            int(op.backlog.enfriamiento_entrada or 300),
        )
        salida.linea(
            "todavía no volvió al mínimo",
            f"desiredCount {final['desiredCount']} (min {op.minimo}): vuelve a lanzar "
            f"hu_w25_recoger a las {_hora(hora)} (sin goteo, solo recoge)",
        )
    columnas = [
        "minuto",
        "deseadas",
        "deseadas_ds",
        "corriendo_ds",
        "tareas_eventos",
        "rtc",
        *(
            f"{c}_{CORTO[cola]}"
            for cola in COLAS
            for c in ("visibles", "no_visibles", "antiguedad")
        ),
        *(f"{c}_{CORTO[cola]}" for cola in COLAS for c in ("enviados", "borrados")),
        *alarmas,
    ]
    salida.tabla(
        "Línea de tiempo (UTC, por minuto)", columnas, [[f.get(c) for c in columnas] for f in filas]
    )
    salida.tabla(
        "Actividades de escalado",
        ["inicio", "estado", "deseadas", "política"],
        [[_hora(a.inicio), a.estado, a.deseadas, a.politica] for a in actividades],
    )
    salida.tabla(
        "Tareas detenidas en el retiro",
        ["tarea", "stoppingAt", "stoppedAt", "s", "stoppedReason", "exitCode"],
        [
            [
                t["id"],
                _hora(t["parada"]),
                _hora(t["detenida"]),
                t["duracion_s"],
                t["stoppedReason"],
                t["exitCode"],
            ]
            for t in detenidas
        ],
    )
    for aviso in avisos:
        salida.linea("aviso", aviso)
    salida.json(
        PREFIJO_RECOGIDA,
        {
            "publicar": publicar.sufijo,
            "declarados": publicar.declarados,
            "goteos": [g.sufijo for g in goteos],
            "goteoDeEstaCorrida": {"sufijo": sufijo_goteo, "eventos": len(goteo)},
            "t0": t0,
            "servicioAlTerminar": {
                "desiredCount": final["desiredCount"],
                "runningCount": final["runningCount"],
            },
            "volvioAlMinimo": volvio,
            "lineaDeTiempo": filas,
            "actividades": actividades,
            "capacidad": capacidad,
            "alarmas": {k: [(t, e) for t, e in v] for k, v in historias.items()},
            "eventosServicio": [
                {"hora": e.get("createdAt"), "mensaje": e.get("message")}
                for e in final.get("events", [])
                if instante(e.get("createdAt")) >= desde
            ],
            "coincidencia": coincidencia,
            "detenidas": detenidas,
            "liberados": liberados,
            "reconciliacion": {
                **asdict(resultado),
                "faltantes": resultado.faltantes[:50],
                "sin_observado": resultado.sin_observado[:50],
            },
            "dlq": {"antes": dlq_antes, "despues": dlq_despues, "ahora": dlq_ahora},
            "decisiones": dict(decisiones),
            "avisos": avisos,
        },
    )

    # Los fallos, después de dejar la evidencia.
    deseadas = [c["a"] for c in capacidad] + [m[1] for m in muestras]
    assert max(deseadas, default=op.minimo) <= op.maximo, f"desiredCount pasó de {op.maximo}"
    assert min(deseadas, default=op.minimo) >= op.minimo, f"desiredCount bajó de {op.minimo}"
    if coincidencia.comparados == 0:
        pytest.fail(
            f"{HISTORIA}: sin minutos con RunningTaskCount y eventos de ECS: no se comprueba H-3"
        )
    if not coincidencia.ok:
        pytest.fail(
            f"{HISTORIA}: RunningTaskCount no coincide con las tareas de los eventos de ECS durante "
            f"{coincidencia.racha_maxima} min (H-3): {coincidencia.discrepancias}"
        )
    if not resultado.ok:
        pytest.fail(
            f"{HISTORIA}: la reconciliación no cierra: faltantes {len(resultado.faltantes)} "
            f"({resultado.faltantes[:10]}), repetidos {resultado.repetidos}, delta de las DLQ "
            f"{resultado.delta_dlq}"
        )


def test_hu_w25_respaldo(request: pytest.FixtureRequest) -> None:
    op = _abrir(request, "hu_w25_respaldo", desde_el_minimo=True)
    aws, salida = op.aws, op.salida

    candidatas = [(op.backlog.nombre, op.backlog.alarma_alta)] + [
        (p.nombre, p.alarma_alta) for p in op.antiguedad.values()
    ]
    ahora = datetime.now(UTC)
    forzadas = [
        alarma
        for _, alarma in candidatas
        if any(
            RAZON_ALARMA in str(i.get("HistoryData", ""))
            for i in _historial(aws, alarma, ahora - VENTANA_AUDITORIA, ahora)
        )
    ]
    politica, nombre = elegir_alarma(candidatas, forzadas)
    alarma = _alarma(aws, nombre)
    if not str(alarma.get("ComparisonOperator")).startswith("GreaterThan"):
        pytest.fail(f"la alarma {nombre} no es la alta: {alarma.get('ComparisonOperator')}")
    tareas = max(int(op.servicio["runningCount"]), 1)
    valor = valor_para_deseadas(float(alarma["Threshold"]), tareas, op.maximo)
    plantilla = plantilla_de(alarma)
    hora, t0 = datetime.now(UTC), time.monotonic()
    enviado = datos_de_estado(alarma, hora, valor)
    salida.linea(
        "respaldo",
        f"DECLARADO: no es carga real. Se fuerza a ALARM la alarma alta {nombre} de {politica} con "
        f"StateReasonData (valor {valor:g} sobre el umbral {float(alarma['Threshold']):g}, pide "
        f"{op.maximo} tareas desde {tareas}); ya forzadas en las últimas 3 h: {forzadas or 'ninguna'}",
    )
    aws.cloudwatch.set_alarm_state(
        AlarmName=nombre, StateValue="ALARM", StateReason=RAZON_ALARMA, StateReasonData=enviado
    )
    muestras: list[tuple[float, int, int]] = []
    limite = t0 + TOPE_RESPALDO_S
    while True:
        servicio = _servicio(aws, op.contexto)
        muestra = (
            round(time.monotonic() - t0, 1),
            int(servicio["desiredCount"]),
            int(servicio["runningCount"]),
        )
        muestras.append(muestra)
        if muestra[1] > op.minimo or time.monotonic() >= limite:
            break
        time.sleep(MUESTREO_S)
    subida = next((m for m in muestras if m[1] > op.minimo), None)
    salida.linea(
        "plantilla",
        f"claves del StateReasonData de la alarma, sin valores: "
        f"{claves_de(plantilla) or 'ninguna utilizable; formato por omisión'}; enviado: {enviado}",
    )
    salida.linea(
        "subida",
        f"desiredCount {op.minimo} → {subida[1]} a los {subida[0]} s"
        if subida
        else f"desiredCount siguió en {op.minimo} durante {TOPE_RESPALDO_S} s",
    )
    salida.linea("siguiente", "lanza hu_w25_recoger: reconstruye la subida y la reducción")
    historial = [
        (_hora(t), e)
        for t, e in cambios_de_alarma(_historial(aws, nombre, hora, datetime.now(UTC)))
    ]
    salida.json(
        PREFIJO_RESPALDO,
        {
            "politica": politica,
            "alarma": nombre,
            "forzadaA": hora,
            "valor": valor,
            "stateReasonData": enviado,
            "muestras": muestras,
            "historial": historial,
            "actividades": _actividades(op, hora - timedelta(minutes=1)),
        },
    )
    if subida is None:
        pytest.fail(
            f"forzar {nombre} no disparó el escalado en {TOPE_RESPALDO_S} s; historial {historial}"
        )
    assert max(m[1] for m in muestras) <= op.maximo, f"desiredCount pasó de {op.maximo}: {muestras}"


# --- Unitarios de las funciones puras (sin AWS) ----------------------------------------------
#
# Se llaman test_hu_w25_unit_*: ningún nombre ni id de parámetro contiene hu_w25_publicar,
# hu_w25_recoger ni hu_w25_respaldo, así que `-k <fase>` selecciona solo la fase.

T = datetime(2026, 11, 22, 14, 0, 0, tzinfo=UTC)
SUFIJO = "20261122140000a1b2"


def _m(minutos: float) -> datetime:
    return T + timedelta(minutes=minutos)


@pytest.mark.parametrize("fase", FASES, ids=["publicar", "recoger", "respaldo"])
def test_hu_w25_unit_seleccionada_solo_con_su_nombre(fase: str) -> None:
    assert seleccionada(fase, fase)
    assert seleccionada(f"{fase} or otra", fase)
    for otra in FASES:
        if otra != fase:
            assert not seleccionada(otra, fase)
    assert not seleccionada("hu_w25", fase)
    assert not seleccionada("", fase)
    assert not seleccionada(None, fase)


def test_hu_w25_unit_id_externo_codifica_la_corrida_la_clase_y_el_total() -> None:
    rafaga = id_externo(SUFIJO, "R", RAFAGA, 7)
    assert rafaga == f"E2E-W25-{SUFIJO}-R2000-00007"
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", rafaga)
    assert leer_id_externo(rafaga) == IdExterno(SUFIJO, "R", 2000, 7)
    goteo = id_externo(SUFIJO, "G", None, 12)
    assert goteo == f"E2E-W25-{SUFIJO}-G-00012"
    assert leer_id_externo(goteo) == IdExterno(SUFIJO, "G", None, 12)
    assert leer_id_externo(id_externo(SUFIJO, "C", 4330, 4329)) == IdExterno(
        SUFIJO, "C", 4330, 4329
    )
    for ajeno in ("IDEAM-2026-1110-0001", f"E2E-W25-{SUFIJO}-X1-00001", "", "E2E-W24-x-R1-00000"):
        assert leer_id_externo(ajeno) is None


@pytest.mark.parametrize(
    ("clase", "total", "n"),
    [("X", 1, 0), ("R", None, 0), ("G", 5, 0), ("R", 10, 10), ("R", 10, -1), ("S", 0, 0)],
    ids=["clase", "sin-total", "goteo-con-total", "n-fuera", "n-negativo", "total-cero"],
)
def test_hu_w25_unit_id_externo_rechaza_lo_invalido(clase: str, total: int | None, n: int) -> None:
    with pytest.raises(ValueError):
        id_externo(SUFIJO, clase, total, n)


def test_hu_w25_unit_evento_id_es_uuid5_de_la_fuente_y_el_id_externo() -> None:
    valor = id_externo(SUFIJO, "S", 1, 0)
    esperado = str(uuid.uuid5(uuid.NAMESPACE_URL, f"telemetria/{FUENTE}/{valor}"))
    assert evento_id_de(valor) == esperado
    assert evento_id_de(valor) != evento_id_de(id_externo(SUFIJO, "S", 1, 0).replace("S1", "R1"))


def test_hu_w25_unit_sobre_observado_cumple_el_esquema() -> None:
    import jsonschema

    esquema = json.loads(ESQUEMA.read_text(encoding="utf-8"))
    validador = jsonschema.Draft202012Validator(
        esquema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    ahora = datetime(2026, 11, 22, 14, 3, 7, 251000, tzinfo=UTC)
    valor = id_externo(SUFIJO, "R", RAFAGA, 0)
    sobre = sobre_observado(valor, SUFIJO, ahora)
    errores = [e.message for e in validador.iter_errors(sobre)]
    assert errores == []
    evento_id = evento_id_de(valor)
    assert sobre["eventId"] == sobre["aggregateId"] == sobre["payload"]["eventoId"] == evento_id
    assert sobre["type"] == OBSERVADO and sobre["version"] == 1
    assert sobre["occurredAt"] == "2026-11-22T14:03:07.251Z"
    assert datetime.fromisoformat(sobre["occurredAt"]) == ahora
    assert sobre["correlationId"] == f"e2e-w25-{SUFIJO}"
    payload = sobre["payload"]
    assert payload["eventoOrigen"] == f"E2E-W25-{SUFIJO}"
    assert (payload["fuente"], payload["idExterno"], payload["zona"]) == (FUENTE, valor, ZONA)
    assert (payload["tipoEvento"], payload["unidad"]) == ("LLUVIA_ACUMULADA", "mm")
    assert payload["observadoEn"] == payload["recibidoEn"] == sobre["occurredAt"]
    assert payload["archivoRef"] == f"e2e-w25/2026/11/22/{evento_id}.json"


def test_hu_w25_unit_sobre_observado_rechaza_una_hora_sin_zona() -> None:
    with pytest.raises(ValueError):
        sobre_observado(id_externo(SUFIJO, "S", 1, 0), SUFIJO, T.replace(tzinfo=None))


def test_hu_w25_unit_lotes_de_diez_en_orden() -> None:
    elementos = list(range(RAFAGA))
    partidos = lotes(elementos)
    assert len(partidos) == 200
    assert all(len(lote) == LOTE for lote in partidos)
    assert [x for lote in partidos for x in lote] == elementos
    assert [len(lote) for lote in lotes(list(range(25)))] == [10, 10, 5]
    assert lotes([]) == []
    assert lotes(list("abc"), 2) == [["a", "b"], ["c"]]
    with pytest.raises(ValueError):
        lotes([1], 0)


def test_hu_w25_unit_ritmo_por_tarea_con_number_of_messages_deleted() -> None:
    ritmo = ritmo_por_tarea(1, 600, 1400)
    assert (ritmo.valor, ritmo.fuente, ritmo.cota_inferior) == (10.0, FUENTE_BORRADOS, False)
    assert ritmo_por_tarea(2, 1200, 800).valor == 10.0  # por tarea
    vaciada = ritmo_por_tarea(1, 2000, 0)
    assert (vaciada.valor, vaciada.cota_inferior) == (pytest.approx(2000 / 60), True)
    assert "600" in ritmo.calculo


def test_hu_w25_unit_ritmo_por_tarea_con_get_queue_attributes() -> None:
    sin_punto = ritmo_por_tarea(1, None, 0)
    assert (sin_punto.valor, sin_punto.fuente, sin_punto.cota_inferior) == (
        pytest.approx(2000 / 60),
        FUENTE_ATRIBUTOS,
        True,
    )
    caida = ritmo_por_tarea(1, None, 1700, Caida(1500, 1200, 30, 210))
    assert (caida.valor, caida.fuente, caida.cota_inferior) == (10.0, FUENTE_ATRIBUTOS, False)
    assert ritmo_por_tarea(1, None, 1700, Caida(300, 0, 30, 210)).cota_inferior
    vacia = ritmo_por_tarea(1, None, 1700, Caida(0, 0, 30, 250))
    assert (vacia.valor, vacia.cota_inferior) == (8.0, True)
    assert ritmo_por_tarea(1, None, 1700, Caida(1200, 1300, 30, 210)).valor == 0.0


def test_hu_w25_unit_ritmo_por_tarea_rechaza_sin_tareas_o_sin_datos() -> None:
    with pytest.raises(ValueError):
        ritmo_por_tarea(0, 600, 1400)
    with pytest.raises(ValueError):
        ritmo_por_tarea(1, None, 1400)
    with pytest.raises(ValueError):
        ritmo_por_tarea(1, None, None)


@pytest.mark.parametrize(
    ("ritmo", "objetivo", "visibles", "esperado"),
    [
        (8, 10, 0, 0),  # (2.000 − 10) / 8 = 248,75 s ≥ 240: la ráfaga sola alcanza
        (8, 80, 0, 0),  # (2.000 − 80) / 8 = 240 s exactos: alcanza
        (0, 10, 0, 0),  # la cola no se mueve: se sostiene sola
        (15, 10, 0, 4330),  # 15 · 240 · 1,2 + 10 = 4.330
        (15, 1, 0, 4330),  # 4.321 sube al siguiente múltiplo de 10
        (15, 10, 10, 4320),  # 4.320 ya es múltiplo: no sube
        (15, 10, 1000, 3330),  # se resta lo que queda en la cola
        (15, 10, 5000, 0),  # lo que queda ya alcanza: nunca negativo
    ],
    ids=[
        "alcanza",
        "limite-exacto",
        "sin-ritmo",
        "intermedio",
        "redondeo",
        "multiplo",
        "resta",
        "nunca-negativo",
    ],
)
def test_hu_w25_unit_complemento(
    ritmo: float, objetivo: float, visibles: int, esperado: int
) -> None:
    resultado = complemento(ritmo, objetivo, visibles)
    assert resultado.eventos == esperado
    assert resultado.eventos % LOTE == 0
    assert not resultado.tope_aplicado
    assert resultado.motivo


@pytest.mark.parametrize("ritmo", [28, 100])
def test_hu_w25_unit_complemento_con_el_tope_de_diez_mil(ritmo: float) -> None:
    resultado = complemento(ritmo, 10, 0)
    assert resultado.eventos == TOPE_TOTAL - RAFAGA == 8000
    assert resultado.tope_aplicado
    assert resultado.necesario is not None and resultado.necesario > 8000
    assert "8000" in resultado.calculo or "8.000" in resultado.calculo


@pytest.mark.parametrize(("ritmo", "objetivo", "visibles"), [(-1, 10, 0), (8, 0, 0), (8, 10, -1)])
def test_hu_w25_unit_complemento_rechaza_lo_invalido(
    ritmo: float, objetivo: float, visibles: int
) -> None:
    with pytest.raises(ValueError):
        complemento(ritmo, objetivo, visibles)


def test_hu_w25_unit_linea_de_tiempo_por_minuto() -> None:
    series = {"rtc": {_m(0): 1.0, _m(2): 2.5}, "visibles": {_m(1) + timedelta(seconds=20): 700}}
    cambios = {
        "deseadas": [(_m(1.5), 3), (_m(-10), 1)],
        "alta": [(_m(2), "ALARM")],
    }
    filas = linea_de_tiempo(_m(0) + timedelta(seconds=10), _m(3), series, cambios)
    assert [f["minuto"] for f in filas] == [_m(0), _m(1), _m(2)]
    assert [f["rtc"] for f in filas] == [1.0, None, 2.5]
    assert [f["visibles"] for f in filas] == [None, 700, None]
    # El valor vigente al cerrar cada minuto: el cambio de las 14:01:30 cuenta desde el 14:01, y
    # uno a las 14:02:00 exactas, desde el 14:02.
    assert [f["deseadas"] for f in filas] == [1, 3, 3]
    assert [f["alta"] for f in filas] == [None, None, "ALARM"]
    assert linea_de_tiempo(_m(3), _m(3), series, cambios) == []
    with pytest.raises(ValueError):
        linea_de_tiempo(_m(0).replace(tzinfo=None), _m(3), series, cambios)


def _tarea(tarea: str, estado: str, **detalle: Any) -> dict[str, Any]:
    return {
        "detail-type": "ECS Task State Change",
        "time": detalle.pop("time", T.isoformat()),
        "detail": {
            "taskArn": f"arn:aws:ecs:us-east-1:000000000000:task/{CLUSTER}/{tarea}",
            "group": f"service:{SERVICIO}",
            "lastStatus": estado,
            **detalle,
        },
    }


def test_hu_w25_unit_tareas_de_eventos_cuenta_las_que_corren_a_mitad_de_minuto() -> None:
    eventos = [
        _tarea("a", "RUNNING", startedAt=_m(-30).isoformat()),
        _tarea("b", "PENDING"),
        _tarea("b", "RUNNING", startedAt=(_m(1) + timedelta(seconds=10)).isoformat()),
        _tarea("c", "RUNNING", startedAt=(_m(1) + timedelta(seconds=50)).isoformat()),
        _tarea(
            "c",
            "STOPPED",
            startedAt=(_m(1) + timedelta(seconds=50)).isoformat(),
            stoppingAt=(_m(3) + timedelta(seconds=20)).isoformat(),
            stoppedAt=(_m(3) + timedelta(seconds=40)).isoformat(),
        ),
        {**_tarea("x", "RUNNING", startedAt=_m(0).isoformat()), "detail-type": "Otra cosa"},
        _tarea("y", "RUNNING", startedAt=_m(0).isoformat(), group="service:auditoria"),
    ]
    assert tareas_de_eventos(eventos, _m(0), _m(5)) == {
        _m(0): 1,
        _m(1): 2,
        _m(2): 3,
        _m(3): 2,
        _m(4): 2,
    }


def test_hu_w25_unit_coinciden_tareas() -> None:
    eventos = {_m(i): 1.0 for i in range(6)}
    assert coinciden_tareas({_m(i): 1.0 for i in range(6)}, eventos).ok
    # Una de diferencia sostenida es la transición de un minuto: se tolera.
    assert coinciden_tareas({_m(i): 2.0 for i in range(6)}, eventos).ok
    # Más de una durante 2 min, no más: se tolera.
    dos = coinciden_tareas({**{_m(i): 1.0 for i in range(6)}, _m(1): 3.0, _m(2): 2.5}, eventos)
    assert dos.ok and dos.racha_maxima == 2 and len(dos.discrepancias) == 2
    # Más de 2 min seguidos (H-3: el doble de tareas): falla.
    tres = coinciden_tareas(
        {**{_m(i): 1.0 for i in range(6)}, **{_m(i): 3.0 for i in (1, 2, 3)}}, eventos
    )
    assert not tres.ok and tres.racha_maxima == 3
    # Con un minuto bueno en medio no hay racha de 3.
    cortada = coinciden_tareas({**{_m(i): 3.0 for i in range(6)}, _m(2): 1.0, _m(4): 1.0}, eventos)
    assert cortada.ok and cortada.racha_maxima == 2 and len(cortada.discrepancias) == 4
    # Un minuto sin datos corta la racha y se cuenta.
    hueco = {_m(0): 3.0, _m(1): 3.0, _m(3): 3.0, _m(4): 3.0, _m(5): 1.0}
    sin = coinciden_tareas(hueco, eventos)
    assert sin.ok and sin.sin_datos == 1 and sin.comparados == 5


def test_hu_w25_unit_reconciliar() -> None:
    esperados = ["a", "b", "c"]
    dlq = {"parametricos-eventos": 2, "auditoria": 0}
    bien = reconciliar(esperados, ["a", "b", "c", "z"], ["a", "b", "c", "z", "z"], dlq, dlq)
    assert bien.ok
    assert (bien.publicados, bien.evaluados, bien.faltantes, bien.repetidos) == (3, 3, [], {})
    assert bien.delta_dlq == {"parametricos-eventos": 0, "auditoria": 0}
    falta = reconciliar(esperados, esperados, ["a", "c"], dlq, dlq)
    assert not falta.ok and falta.faltantes == ["b"]
    repetido = reconciliar(esperados, esperados, ["a", "b", "c", "b"], dlq, dlq)
    assert not repetido.ok and repetido.repetidos == {"b": 2}
    sin_observado = reconciliar(esperados, ["a"], esperados, dlq, dlq)
    assert sin_observado.ok and sin_observado.sin_observado == ["b", "c"]
    crece = reconciliar(esperados, esperados, esperados, dlq, {**dlq, "auditoria": 1})
    assert not crece.ok and crece.delta_dlq["auditoria"] == 1
    baja = reconciliar(esperados, esperados, esperados, dlq, {**dlq, "parametricos-eventos": 0})
    assert baja.ok and baja.delta_dlq["parametricos-eventos"] == -2
    with pytest.raises(ValueError):
        reconciliar([], [], [], dlq, dlq)
    with pytest.raises(ValueError):
        reconciliar(esperados, esperados, esperados, dlq, {"auditoria": 0})


def test_hu_w25_unit_ventana_de_retiro() -> None:
    reduccion = _m(20)
    con_tarea = ventana_de_retiro(
        reduccion, [(reduccion + timedelta(seconds=5), reduccion + timedelta(seconds=12))]
    )
    assert con_tarea == (_m(19), reduccion + timedelta(seconds=72))
    assert ventana_de_retiro(reduccion, []) == (_m(19), reduccion + timedelta(seconds=90))
    antes = ventana_de_retiro(reduccion, [(reduccion - timedelta(seconds=30), None)])
    assert antes == (reduccion - timedelta(seconds=90), reduccion + timedelta(seconds=90))
    with pytest.raises(ValueError):
        ventana_de_retiro(reduccion.replace(tzinfo=None), [])


def test_hu_w25_unit_hora_para_recoger() -> None:
    ahora = _m(5) + timedelta(seconds=10)
    # Sin el momento en que la cola bajó del objetivo: 15 puntos y 2 min de margen desde ahora.
    assert hora_para_recoger(ahora, None, None, 300) == _m(23)
    assert hora_para_recoger(ahora, _m(0), None, 300) == _m(17)
    # Tras una reducción parcial, su enfriamiento de entrada y el margen.
    assert hora_para_recoger(ahora, _m(0), _m(20), 300) == _m(27)
    # Nunca antes de ahora.
    assert hora_para_recoger(ahora, _m(-120), None, 300) == _m(6)
    with pytest.raises(ValueError):
        hora_para_recoger(ahora.replace(tzinfo=None), None, None, 300)


RECURSO = f"TargetTracking-service/{CLUSTER}/{SERVICIO}"
ALTA = f"{RECURSO}-AlarmHigh-fc0e4183-23ac-497e-9992-691c9980c38e"
BAJA = f"{RECURSO}-AlarmLow-61a39305-ed6c-4ff8-b3f8-2b1c5e3ad1a2"
ALTA_EVENTOS = f"{RECURSO}-AlarmHigh-0d1c55c7-7a1e-4b8e-9d7b-5a5b0e2c3f10"


def _politica(
    nombre: str,
    alarmas: list[str],
    metricas: list[str],
    tipo: str = "TargetTrackingScaling",
    **config: Any,
) -> dict[str, Any]:
    return {
        "PolicyName": nombre,
        "PolicyType": tipo,
        "Alarms": [
            {"AlarmName": a, "AlarmARN": f"arn:aws:cloudwatch:x:alarm:{a}"} for a in alarmas
        ],
        "TargetTrackingScalingPolicyConfiguration": {
            "TargetValue": 10.0,
            "ScaleOutCooldown": 60,
            "ScaleInCooldown": 300,
            "CustomizedMetricSpecification": {
                "Metrics": [
                    {"Id": f"m{i}", "MetricStat": {"Metric": {"MetricName": m}}}
                    for i, m in enumerate(metricas)
                ]
            },
            **config,
        },
    }


def test_hu_w25_unit_politica_de_la_backlog_por_tarea() -> None:
    nombre = f"{CLUSTER}-{SERVICIO}-backlog"
    metricas = ["ApproximateNumberOfMessagesVisible"] * 2 + ["RunningTaskCount"]
    respuesta = {
        "ScalingPolicies": [_politica(nombre, [BAJA, ALTA], metricas), _politica("otra", [], [])]
    }
    politica = politica_de(respuesta, nombre)
    assert politica == Politica(nombre, 10.0, 60, 300, False, tuple(metricas), ALTA, BAJA)
    assert politica.por_tarea
    suma = politica_de({"ScalingPolicies": [_politica(nombre, [ALTA, BAJA], metricas[:2])]}, nombre)
    assert not suma.por_tarea


def test_hu_w25_unit_politica_de_la_antiguedad_sin_alarma_baja() -> None:
    nombre = f"{CLUSTER}-{SERVICIO}-antiguedad-parametricos-eventos"
    sola = _politica(nombre, [ALTA_EVENTOS], [], DisableScaleIn=True)
    sola["TargetTrackingScalingPolicyConfiguration"]["CustomizedMetricSpecification"] = {
        "MetricName": "ApproximateAgeOfOldestMessage"
    }
    politica = politica_de({"ScalingPolicies": [sola]}, nombre, exige_baja=False)
    assert (politica.alarma_alta, politica.alarma_baja, politica.desactiva_entrada) == (
        ALTA_EVENTOS,
        None,
        True,
    )
    assert politica.metricas == ("ApproximateAgeOfOldestMessage",)
    with pytest.raises(ValueError):
        politica_de({"ScalingPolicies": [sola]}, nombre)


@pytest.mark.parametrize(
    ("politicas", "error"),
    [
        ([], LookupError),
        ([_politica("p", [ALTA, BAJA], []), _politica("p", [ALTA, BAJA], [])], LookupError),
        ([_politica("p", [ALTA, BAJA], [], tipo="StepScaling")], ValueError),
        ([_politica("p", [ALTA, ALTA_EVENTOS, BAJA], [])], ValueError),
        ([_politica("p", [BAJA], [])], ValueError),
    ],
    ids=["falta", "dos", "no-target-tracking", "dos-altas", "sin-alta"],
)
def test_hu_w25_unit_politica_de_rechaza(politicas: list[dict[str, Any]], error: type) -> None:
    with pytest.raises(error):
        politica_de({"ScalingPolicies": politicas}, "p")


def _salidas(**cambios: Any) -> dict[str, Any]:
    colas = {
        nombre: {
            "name": f"{CLUSTER}-{nombre}",
            "url": f"https://sqs.us-east-1.amazonaws.com/000000000000/{CLUSTER}-{nombre}",
            "dlq_name": f"{CLUSTER}-{nombre}-dlq",
        }
        for nombre in (*COLAS, "auditoria")
    }
    plataforma = {
        "environment": "int",
        "prefix": CLUSTER,
        "region": "us-east-1",
        "cluster": {"name": CLUSTER},
        "events_topic_arn": "arn:aws:sns:us-east-1:000000000000:solventa-int-eventos-negocio",
        "audit_bucket": {"name": "solventa-int-auditoria"},
        "events_log_group": f"/ecs/{CLUSTER}/events",
        "queues": colas,
        **cambios,
    }
    return {"platform": plataforma}


def test_hu_w25_unit_contexto_de_las_salidas_de_platform() -> None:
    contexto = contexto_de(_salidas())
    assert (contexto.cluster, contexto.prefijo, contexto.region) == (CLUSTER, CLUSTER, "us-east-1")
    assert contexto.topico.endswith("eventos-negocio")
    assert (contexto.bucket, contexto.eventos_ecs) == (
        "solventa-int-auditoria",
        f"/ecs/{CLUSTER}/events",
    )
    assert set(contexto.colas) == {*COLAS, "auditoria"}
    assert contexto.colas["auditoria"]["dlq_name"] == f"{CLUSTER}-auditoria-dlq"


@pytest.mark.parametrize(
    "salidas",
    [
        {},
        _salidas(environment="exp"),
        _salidas(cluster={"name": "solventa-exp"}),
        _salidas(prefix="solventa-exp"),
        _salidas(events_topic_arn=""),
        _salidas(audit_bucket={}),
        _salidas(events_log_group=None),
        _salidas(queues={}),
    ],
    ids=["sin-salidas", "ambiente", "cluster", "prefijo", "topico", "bucket", "eventos", "colas"],
)
def test_hu_w25_unit_contexto_de_rechaza_salidas_incompletas_o_de_otro_ambiente(
    salidas: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        contexto_de(salidas)


def _observado(valor: str, momento: datetime, como_texto: bool = True) -> dict[str, Any]:
    sobre = sobre_observado(valor, valor.split("-")[2], momento)
    evento_id = sobre["eventId"]
    return {
        "accion": OBSERVADO,
        "resourceId": evento_id,
        "eventId": evento_id,
        "timestamp": sobre["occurredAt"],
        "evento": json.dumps(sobre) if como_texto else sobre,
    }


def test_hu_w25_unit_corridas_y_esperados_desde_la_auditoria() -> None:
    vieja, actual, goteo = "20261122120000aaaa", SUFIJO, "20261122143000bbbb"
    registros = [
        _observado(id_externo(vieja, "R", 3, 0), _m(-120)),
        _observado(id_externo(actual, "S", 1, 0), _m(-2)),
        _observado(id_externo(actual, "R", 3, 2), _m(1)),
        _observado(id_externo(actual, "R", 3, 0), _m(0), como_texto=False),
        _observado(id_externo(actual, "C", 2, 1), _m(2)),
        _observado(id_externo(goteo, "G", None, 4), _m(30)),
        {"accion": EVALUADO, "resourceId": "x", "evento": "{}", "timestamp": T.isoformat()},
        {
            "accion": OBSERVADO,
            "resourceId": "y",
            "evento": "no es JSON",
            "timestamp": T.isoformat(),
        },
        {"accion": OBSERVADO, "resourceId": "z", "evento": {"payload": {"idExterno": "IDEAM-1"}}},
    ]
    corridas = corridas_de(registros)
    assert set(corridas) == {vieja, actual, goteo}
    assert corridas[actual].declarados == {"S": 1, "R": 3, "C": 2}
    assert corridas[actual].observados["R"] == {id_externo(actual, "R", 3, n) for n in (0, 2)}
    assert corridas[actual].primeras["R"] == _m(0)
    publicar, goteos, ids = esperados_de(corridas)
    assert publicar.sufijo == actual
    assert [g.sufijo for g in goteos] == [goteo]
    # Los declarados se reconstruyen enteros (R-00001 y C-00000 faltan en la auditoría); el goteo,
    # por lo observado.
    esperados = [
        id_externo(actual, "S", 1, 0),
        *(id_externo(actual, "R", 3, n) for n in range(3)),
        *(id_externo(actual, "C", 2, n) for n in range(2)),
        id_externo(goteo, "G", None, 4),
    ]
    assert ids == [evento_id_de(v) for v in esperados]
    with pytest.raises(LookupError):
        esperados_de({goteo: corridas[goteo]})


def test_hu_w25_unit_actividades_de() -> None:
    politica = f"{CLUSTER}-{SERVICIO}-backlog"
    items = [
        {
            "StartTime": _m(9),
            "EndTime": _m(10),
            "StatusCode": "Successful",
            "Description": "Setting desired count to 1.",
            "Cause": f"monitor alarm {BAJA} in state ALARM triggered policy {politica}",
        },
        {
            "StartTime": _m(4),
            "StatusCode": "InProgress",
            "Description": "Setting desired count to 3.",
            "Cause": f"monitor alarm {ALTA} in state ALARM triggered policy {politica}",
        },
        {"StartTime": _m(5).isoformat(), "StatusCode": "Failed", "Description": "otra"},
    ]
    actividades = actividades_de(items)
    assert [(a.inicio, a.deseadas, a.estado) for a in actividades] == [
        (_m(4), 3, "InProgress"),
        (_m(5), None, "Failed"),
        (_m(9), 1, "Successful"),
    ]
    assert actividades[0].politica == politica
    assert (actividades[0].fin, actividades[1].politica) == (None, None)


def test_hu_w25_unit_tareas_detenidas() -> None:
    contenedores = [{"name": SERVICIO, "exitCode": 143}]
    razon = "Scaling activity initiated by (deployment ecs-svc/1)"
    detenida = (_m(20) + timedelta(seconds=12)).isoformat()
    eventos = [
        _tarea(
            "a",
            "STOPPED",
            stoppingAt=_m(20).isoformat(),
            stoppedAt=detenida,
            stoppedReason=razon,
            stopCode="ServiceSchedulerInitiated",
            containers=contenedores,
        ),
        _tarea("a", "STOPPED", stoppingAt=_m(20).isoformat(), stoppedAt=detenida),
        _tarea("b", "STOPPED", stoppedAt=_m(-60).isoformat()),
        _tarea("c", "RUNNING", startedAt=_m(1).isoformat()),
        _tarea("d", "STOPPED", stoppedAt=_m(25).isoformat(), group="service:auditoria"),
    ]
    assert tareas_detenidas(eventos, _m(0)) == [
        {
            "id": "a",
            "parada": _m(20),
            "detenida": _m(20) + timedelta(seconds=12),
            "duracion_s": 12.0,
            "stoppedReason": razon,
            "stopCode": "ServiceSchedulerInitiated",
            "exitCode": {SERVICIO: 143},
        }
    ]


def _fila(minuto: float, eventos: float | None, polizas: float, rtc: float) -> dict[str, Any]:
    return {
        "minuto": _m(minuto),
        "visibles_eventos": eventos,
        "visibles_polizas": polizas,
        "rtc": rtc,
    }


def test_hu_w25_unit_bajo_objetivo_desde() -> None:
    filas = [
        _fila(0, 2000, 0, 1),
        _fila(1, 1500, 0, 1),
        _fila(2, 27, 0, 3),
        _fila(3, 26, 1, 3),  # 27 / 3 = 9: el 90 % de 10, todavía no está por debajo
        _fila(4, 3, 0, 3),
        _fila(5, None, 0, 3),
        _fila(6, 0, 0, 3),
    ]
    assert bajo_objetivo_desde(filas, 10) == _m(4)
    assert bajo_objetivo_desde(filas[:4], 10) is None  # sigue en el 90 % o más
    assert bajo_objetivo_desde([_fila(0, 1, 0, 1)], 10) is None  # nunca estuvo en el 90 %


def test_hu_w25_unit_valor_para_deseadas_pide_justo_esas_tareas() -> None:
    assert valor_para_deseadas(10.0, 1, 3) == 27.5
    for umbral in (10.0, 100.0, 180.0):
        for tareas, deseadas in ((1, 2), (1, 3), (2, 3)):
            valor = valor_para_deseadas(umbral, tareas, deseadas)
            assert valor > umbral
            assert math.ceil(tareas * valor / umbral) == deseadas
            assert round(tareas * valor / umbral) == deseadas
    for tareas, deseadas in ((0, 3), (2, 2), (3, 1)):
        with pytest.raises(ValueError):
            valor_para_deseadas(10.0, tareas, deseadas)


def test_hu_w25_unit_elegir_alarma() -> None:
    candidatas = [("backlog", "a1"), ("antiguedad-eventos", "a2"), ("antiguedad-polizas", "a3")]
    assert elegir_alarma(candidatas, []) == ("backlog", "a1")
    assert elegir_alarma(candidatas, ["a1"]) == ("antiguedad-eventos", "a2")
    assert elegir_alarma(candidatas, {"a1", "a2"}) == ("antiguedad-polizas", "a3")
    assert elegir_alarma(candidatas, ["a1", "a2", "a3"]) == ("backlog", "a1")
    with pytest.raises(ValueError):
        elegir_alarma([], [])


def test_hu_w25_unit_tabla_markdown() -> None:
    assert tabla(["a", "b"], [[1, None], ["x|y", "l1\nl2"]]) == (
        "| a | b |\n| --- | --- |\n| 1 | — |\n| x\\|y | l1 l2 |"
    )
    assert tabla(["minuto"], [[_m(0)]]) == "| minuto |\n| --- |\n| 14:00 |"


# Copias de los unitarios de W08 (tests/e2e/test_hu_w08_escalado.py) para plantilla_de, claves_de y
# datos_de_estado, con la alarma alta de la política -backlog de pagos-parametricos.
ALARMA_ALTA = {
    "AlarmName": ALTA,
    "ComparisonOperator": "GreaterThanThreshold",
    "Threshold": 10.0,
    "Statistic": "Average",
    "Period": 60,
    "EvaluationPeriods": 3,
}
AHORA = datetime(2026, 11, 20, 14, 32, 52, 31000, tzinfo=UTC)
PLANTILLA = {
    "version": "1.0",
    "queryDate": "2026-11-22T09:10:41.502+0000",
    "startDate": "2026-11-22T09:07:00.000+0000",
    "statistic": "Average",
    "period": 60,
    "recentDatapoints": [0.0, 1.0, 0.0],
    "threshold": 10.0,
    "evaluatedDatapoints": [
        {"timestamp": "2026-11-22T09:09:00.000+0000", "sampleCount": 3.0, "value": 0.0},
        {"timestamp": "2026-11-22T09:08:00.000+0000", "sampleCount": 3.0, "value": 1.0},
    ],
}
PLANTILLA_SIN_EVALUADOS = {k: v for k, v in PLANTILLA.items() if k != "evaluatedDatapoints"}


def _con_plantilla(plantilla: Any, **cambios: Any) -> dict[str, Any]:
    texto = plantilla if isinstance(plantilla, str) else json.dumps(plantilla)
    return {**ALARMA_ALTA, "StateReasonData": texto, **cambios}


@pytest.mark.parametrize(
    "plantilla",
    [None, "", "no es JSON", '{"version": "1.0",', "[1, 2]", '"texto"', '{"version": "1.0"}'],
    ids=["falta", "vacia", "texto", "cortada", "lista", "cadena", "sin-recentDatapoints"],
)
def test_hu_w25_unit_datos_de_estado_sin_plantilla_usa_el_formato_por_omision(
    plantilla: str | None,
) -> None:
    alarma = dict(ALARMA_ALTA) if plantilla is None else _con_plantilla(plantilla)
    assert plantilla_de(alarma) is None
    datos = json.loads(datos_de_estado(alarma, AHORA, 27.5))
    assert datos == {
        "version": "1.0",
        "queryDate": "2026-11-20T14:32:52.031+0000",
        "startDate": "2026-11-20T14:31:00.000+0000",
        "statistic": "Average",
        "period": 60,
        "recentDatapoints": [27.5],
        "threshold": 10.0,
        "evaluatedDatapoints": [
            {"timestamp": "2026-11-20T14:31:00.000+0000", "sampleCount": 1.0, "value": 27.5}
        ],
    }


def test_hu_w25_unit_datos_de_estado_conserva_las_claves_de_la_plantilla() -> None:
    alarma = _con_plantilla(PLANTILLA, Statistic="Maximum", Period=300, Threshold=180.0)
    datos = json.loads(datos_de_estado(alarma, AHORA, 495.0))
    assert list(datos) == list(PLANTILLA)
    assert datos["startDate"] == "2026-11-20T14:25:00.000+0000"
    assert (datos["statistic"], datos["period"], datos["threshold"]) == ("Maximum", 300, 180.0)
    assert datos["recentDatapoints"] == [495.0]
    assert datos["evaluatedDatapoints"] == [
        {"timestamp": "2026-11-20T14:25:00.000+0000", "sampleCount": 3.0, "value": 495.0}
    ]
    otra_zona = AHORA.astimezone(timezone(timedelta(hours=-5)))
    assert json.loads(datos_de_estado(alarma, otra_zona, 495.0))["queryDate"] == (
        "2026-11-20T14:32:52.031+0000"
    )
    sin = json.loads(datos_de_estado(_con_plantilla(PLANTILLA_SIN_EVALUADOS), AHORA, 27.5))
    assert list(sin) == list(PLANTILLA_SIN_EVALUADOS)


def test_hu_w25_unit_datos_de_estado_claves_ajenas_y_sample_count_por_omision() -> None:
    plantilla = {
        **PLANTILLA,
        "evaluatedDatapoints": [{"timestamp": "2026-11-22T09:09:00.000+0000", "value": 3.4}],
        "otraClave": {"x": 1},
    }
    datos = json.loads(datos_de_estado(_con_plantilla(plantilla), AHORA, 27.5))
    assert datos["otraClave"] == {"x": 1}
    assert datos["evaluatedDatapoints"] == [
        {"timestamp": "2026-11-20T14:31:00.000+0000", "value": 27.5, "sampleCount": 1.0}
    ]


def test_hu_w25_unit_claves_de_la_plantilla_sin_valores() -> None:
    assert claves_de(None) is None
    assert claves_de(PLANTILLA) == [
        "version",
        "queryDate",
        "startDate",
        "statistic",
        "period",
        "recentDatapoints",
        "threshold",
        "evaluatedDatapoints[timestamp, sampleCount, value]",
    ]


@pytest.mark.parametrize(
    "alarma",
    [
        {k: v for k, v in ALARMA_ALTA.items() if k != "Threshold"},
        {k: v for k, v in ALARMA_ALTA.items() if k != "Period"},
        {k: v for k, v in ALARMA_ALTA.items() if k != "Statistic"},
        _con_plantilla({**PLANTILLA, "relleno": "x" * 4000}),
    ],
    ids=["sin-umbral", "sin-periodo", "sin-estadistica", "mas-de-4000"],
)
def test_hu_w25_unit_datos_de_estado_rechaza_alarmas_incompletas_o_largas(
    alarma: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        datos_de_estado(alarma, AHORA, 27.5)


@pytest.mark.parametrize("valor", [10.0, 9.9])
def test_hu_w25_unit_datos_de_estado_rechaza_un_valor_que_no_supera_el_umbral(
    valor: float,
) -> None:
    with pytest.raises(ValueError):
        datos_de_estado(ALARMA_ALTA, AHORA, valor)


def test_hu_w25_unit_datos_de_estado_rechaza_una_hora_sin_zona() -> None:
    with pytest.raises(ValueError):
        datos_de_estado(ALARMA_ALTA, AHORA.replace(tzinfo=None), 27.5)


# Auxiliares escritas con las fases (después del código): las cubre una mutación cada una.


def test_hu_w25_unit_cambios_de_capacidad() -> None:
    def actividad(minuto: float, deseadas: int | None, estado: str = "Successful") -> Actividad:
        return Actividad(_m(minuto), None, estado, deseadas, f"{CLUSTER}-{SERVICIO}-backlog", "")

    actividades = [
        actividad(4, 3),
        actividad(5, 3),  # sin cambio
        actividad(6, 2, "Failed"),
        actividad(7, None),
        actividad(20, 1),
    ]
    assert cambios_de_capacidad(actividades, 1) == [
        {"hora": _m(4), "de": 1, "a": 3, "sentido": "subida", "politica": actividades[0].politica},
        {
            "hora": _m(20),
            "de": 3,
            "a": 1,
            "sentido": "reducción",
            "politica": actividades[0].politica,
        },
    ]
    assert cambios_de_capacidad([], 1) == []


def test_hu_w25_unit_cambios_de_alarma() -> None:
    items = [
        {
            "Timestamp": _m(5),
            "HistoryData": json.dumps({"newState": {"stateValue": "OK"}}),
            "HistorySummary": "Alarm updated from ALARM to OK",
        },
        {"Timestamp": _m(1), "HistoryData": "no es JSON", "HistorySummary": "from OK to ALARM"},
        {"Timestamp": _m(3), "HistoryData": "{}", "HistorySummary": "sin estado"},
    ]
    assert cambios_de_alarma(items) == [(_m(1), "ALARM"), (_m(5), "OK")]


def test_hu_w25_unit_instante_y_nuevo_sufijo() -> None:
    assert instante("2026-11-22T14:00:00Z") == T
    assert instante("2026-11-22T09:00:00-05:00") == T
    assert instante(T.replace(tzinfo=None)) == T
    with pytest.raises(ValueError):
        instante("ayer")
    with pytest.raises(ValueError):
        instante(None)
    sufijo = nuevo_sufijo(T.astimezone(timezone(timedelta(hours=-5))))
    assert re.fullmatch(r"20261122140000[0-9a-f]{4}", sufijo)
    assert leer_id_externo(id_externo(sufijo, "R", RAFAGA, 0)) == IdExterno(sufijo, "R", RAFAGA, 0)
    assert nuevo_sufijo(T) < nuevo_sufijo(_m(1))  # ordena las corridas por hora


def test_hu_w25_unit_limitar_el_resumen() -> None:
    assert limitar("corto", 100) == "corto"
    largo = limitar("ñ" * 200, 100)
    assert len(largo.encode("utf-8")) <= 100
    assert largo.endswith("(recortado: el JSON completo está en el log del job)")
