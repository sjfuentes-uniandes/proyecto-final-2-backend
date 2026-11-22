"""N2 (HU-W08) en INT: fuerza la alarma alta de la política de CPU de `cotizacion` y observa el escalado.

**Hace que App Auto Scaling cambie el número de tareas de `cotizacion` en INT.** Solo corre si la
expresión `-k` de la sesión contiene `hu_w08`; con el filtro vacío (todas las E2E) se omite siempre,
también con `E2E_REQUERIDO=1`. Configuración, precondiciones y cuándo es seguro lanzarla:
infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)"). No usa el portal ni el usuario E2E.

Guardas, antes de tocar AWS:
1. `-k` con `hu_w08`.
2. `ENV=int`. Solo el clúster de ese ambiente (salida `platform.cluster.name` de infra/platform; por
   convención `solventa-int`, `infra/platform/main.tf:34`) y el servicio `cotizacion`.
3. boto3 y credenciales: sin ellas se omite, o falla con `E2E_REQUERIDO=1`.
4. Existe la política `<prefijo>-cotizacion-cpu` (`infra/apps/autoscaling.tf`), el scalable target
   tiene min < max y escalado hacia fuera activo, y `desiredCount == min`. Si no, se omite con el
   motivo (o falla con `E2E_REQUERIDO=1`) sin tocar nada.

Recorrido: lee la política y sus alarmas (`politica_de`, sin construir el nombre de la alarma),
lleva la alarma alta a `ALARM` con `set_alarm_state` y un `StateReasonData` que pide una sola tarea
más (`datos_de_estado` y `valor_para_una_tarea_mas`; sin él, App Auto Scaling no ejecuta la política).
Su plantilla es el `StateReasonData` que CloudWatch dejó en la alarma alta al evaluarla
(`plantilla_de`, camelCase minúscula): se conservan sus claves y se cambian los valores. Después
muestrea `desiredCount` y `runningCount`:
(a) AC1: `desiredCount` sube en `E2E_W08_ESPERA_SUBIDA_S` y nunca pasa de `MaxCapacity`. Si no sube,
    falla con lo observado: es el riesgo del plan (target tracking puede ignorar la alarma forzada).
(b) AC2: vuelve a `MinCapacity` en `E2E_W08_ESPERA_BAJADA_S` sin bajar de él. Si no vuelve a
    tiempo no falla: lo devolverá el autoescalado tras el enfriamiento y la alarma baja.
(c) AC3: la tarea retirada, con su `stoppedReason` y el `exitCode` de cada contenedor (143 tras el
    apagado ordenado de HU-W07, docs/hu-w07-replicas.md §3.3).
Nunca cambia el scalable target ni la política, y no vuelve a tocar la alarma: vuelve sola a su
estado en la siguiente evaluación.

Las funciones puras (`seleccionada`, `politica_de`, `linea_de_tiempo`, `valor_para_una_tarea_mas`,
`plantilla_de`, `claves_de`, `datos_de_estado`) tienen sus unitarios al final de este archivo y no tocan AWS.
"""

import json
import math
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

HISTORIA = "HU-W08"
FILTRO = "hu_w08"
MOTIVO_FILTRO = (
    "HU-W08 N2: fuerza la alarma de escalado de cotizacion en INT; selecciónala con -k hu_w08"
)
AMBIENTE = "int"
SERVICIO = "cotizacion"
DIMENSION = "ecs:service:DesiredCount"
RAZON_ALARMA = "HU-W08 N2: alarma alta forzada para demostrar el escalado hacia fuera"
ESPERA_SUBIDA_S = int(os.getenv("E2E_W08_ESPERA_SUBIDA_S", "180"))
# El job de .github/workflows/e2e.yml tiene `timeout-minutes: 20` (1200 s): unos 4 min de
# preparación (checkout, uv, terraform init de platform) + 180 s de subida + 600 s de bajada deja
# unos 3 min de margen para las guardas, el resumen y el `finally`.
ESPERA_BAJADA_S = int(os.getenv("E2E_W08_ESPERA_BAJADA_S", "600"))
MUESTREO_S = float(os.getenv("E2E_W08_MUESTREO_S", "10"))
# App Auto Scaling nombra las alarmas de target tracking
# `TargetTracking-<ResourceId>-AlarmHigh-<uuid>` y `...-AlarmLow-<uuid>`.
_ALARMA = re.compile(r"-Alarm(High|Low)-[0-9A-Fa-f-]+$")


# --- Funciones puras (con unitarios al final) --------------------------------------------------


def seleccionada(keyword: str) -> bool:
    """Si la sesión eligió esta prueba a propósito: `-k` con `hu_w08`."""
    return FILTRO in (keyword or "")


@dataclass(frozen=True)
class Politica:
    nombre: str
    objetivo: float
    enfriamiento_salida: int | None
    enfriamiento_entrada: int | None
    alarma_alta: str
    alarma_baja: str


def politica_de(respuesta: dict[str, Any], nombre: str) -> Politica:
    """La política `nombre` de una respuesta de `DescribeScalingPolicies`, con su alarma alta y su
    alarma baja tal como las devuelve la API en `Alarms` (se eligen por el sufijo `-AlarmHigh-` o
    `-AlarmLow-` del nombre devuelto; el nombre nunca se construye).

    `LookupError` si la política no está; `ValueError` si no es de target tracking o no tiene
    exactamente una alarma alta y una baja."""
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
    if len(altas) != 1 or len(bajas) != 1:
        raise ValueError(f"alarmas de {nombre!r}: altas {altas}, bajas {bajas}")
    config = politica.get("TargetTrackingScalingPolicyConfiguration") or {}
    return Politica(
        nombre,
        float(config["TargetValue"]),
        config.get("ScaleOutCooldown"),
        config.get("ScaleInCooldown"),
        altas[0],
        bajas[0],
    )


@dataclass(frozen=True)
class Muestra:
    t: float  # segundos desde que se forzó la alarma
    deseadas: int
    corriendo: int
    pendientes: int


def linea_de_tiempo(muestras: Iterable[Muestra]) -> list[Muestra]:
    """Las muestras en las que cambia (deseadas, corriendo, pendientes), más la última."""
    lista = list(muestras)
    cambios = [
        m
        for i, m in enumerate(lista)
        if i == 0
        or (m.deseadas, m.corriendo, m.pendientes)
        != (lista[i - 1].deseadas, lista[i - 1].corriendo, lista[i - 1].pendientes)
    ]
    if lista and cambios[-1] is not lista[-1]:
        cambios.append(lista[-1])
    return cambios


def valor_para_una_tarea_mas(umbral: float, tareas: int) -> float:
    """El valor de CPU que se informa al forzar la alarma alta para que target tracking pida una
    sola tarea más que `tareas`.

    Target tracking ajusta la capacidad en proporción a la métrica (capacidad × valor / objetivo) y
    redondea; en la alarma alta, el umbral es el objetivo. Con `umbral × (tareas + 0,75) / tareas`
    la cuenta da `tareas + 0,75`, que es `tareas + 1` tanto si redondea hacia arriba como si redondea
    al más cercano (de 2 tareas con objetivo 60: 82,5 % → 2,75 → 3, sin llegar a 4)."""
    if tareas < 1:
        raise ValueError(f"hacen falta tareas >= 1 (tareas = {tareas})")
    return umbral * (tareas + 0.75) / tareas


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

    App Auto Scaling lo exige cuando la alarma invoca una política ("StateReasonData must be
    provided") y calcula el ajuste con él; con las claves del ejemplo de la API `SetAlarmState`
    (`Version`, `QueryDate`...) no ejecuta la acción ("Metric data points must be provided", N2 del
    2026-11-22). Por eso la plantilla es el `StateReasonData` que CloudWatch dejó en la alarma al
    evaluarla (`plantilla_de`) o, si no sirve, `FORMATO_POR_OMISION`: se conservan sus claves y su
    estructura y se reemplazan solo los valores. `queryDate` es `ahora`; `startDate`, el inicio del
    periodo anterior al de la consulta; `recentDatapoints`, `[valor]`; `evaluatedDatapoints` (si la
    plantilla lo trae), un único punto de ese periodo con el `sampleCount` de la plantilla (o 1.0);
    `statistic`, `period` y `threshold` salen de la alarma. El resto se deja como está.

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


# --- Guardas ---------------------------------------------------------------------------------


def _requerido(motivo: str) -> None:
    """Falta de configuración o precondición: falla con E2E_REQUERIDO=1, si no se omite."""
    if os.getenv("E2E_REQUERIDO") == "1":
        pytest.fail(motivo)
    pytest.skip(motivo)


def _cluster(salidas: dict[str, Any]) -> tuple[str, str]:
    """El clúster y el prefijo de INT (salidas `platform.cluster.name` y `platform.prefix`)."""
    plataforma = salidas.get("platform", {})
    if plataforma and plataforma.get("environment") != AMBIENTE:
        pytest.fail(
            f"las salidas de infra/platform son de {plataforma.get('environment')!r}, no de int"
        )
    esperado = f"solventa-{AMBIENTE}"
    nombre: str = plataforma.get("cluster", {}).get("name") or esperado
    prefijo: str = plataforma.get("prefix") or esperado
    if nombre != esperado or prefijo != esperado:
        pytest.fail(f"clúster o prefijo inesperados para int: {nombre!r}, {prefijo!r}")
    return nombre, prefijo


@dataclass(frozen=True)
class Aws:
    autoscaling: Any
    cloudwatch: Any
    ecs: Any


def _clientes_aws(recurso: str) -> tuple[Aws, list[dict[str, Any]]]:
    """Los clientes y el scalable target de `recurso`. La lectura comprueba las credenciales."""
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover
        _requerido(f"N2 {HISTORIA}: boto3 no está instalado")
    region = os.getenv("AWS_REGION", "us-east-1")
    try:
        aws = Aws(
            boto3.client("application-autoscaling", region_name=region),
            boto3.client("cloudwatch", region_name=region),
            boto3.client("ecs", region_name=region),
        )
        objetivos = aws.autoscaling.describe_scalable_targets(
            ServiceNamespace="ecs", ResourceIds=[recurso], ScalableDimension=DIMENSION
        )["ScalableTargets"]
    except (BotoCoreError, ClientError) as error:
        _requerido(f"N2 {HISTORIA}: sin acceso a AWS ({type(error).__name__})")
    return aws, list(objetivos)


# --- AWS (solo lecturas, salvo `set_alarm_state` en la prueba) --------------------------------


def _servicio(ecs: Any, cluster: str) -> dict[str, Any]:
    (servicio,) = ecs.describe_services(cluster=cluster, services=[SERVICIO])["services"]
    return dict(servicio)


def _muestra(ecs: Any, cluster: str, t0: float) -> Muestra:
    servicio = _servicio(ecs, cluster)
    return Muestra(
        round(time.monotonic() - t0, 1),
        servicio["desiredCount"],
        servicio["runningCount"],
        servicio["pendingCount"],
    )


def _observar(
    ecs: Any,
    cluster: str,
    t0: float,
    tope_s: float,
    muestras: list[Muestra],
    listo: Callable[[Muestra], bool],
) -> bool:
    """Muestrea cada MUESTREO_S hasta `listo` o el tope. Las muestras se agregan a `muestras`."""
    limite = time.monotonic() + tope_s
    while True:
        muestra = _muestra(ecs, cluster, t0)
        muestras.append(muestra)
        if listo(muestra):
            return True
        if time.monotonic() >= limite:
            return False
        time.sleep(MUESTREO_S)


def _hora(valor: Any) -> str:
    return valor.isoformat(timespec="seconds") if isinstance(valor, datetime) else str(valor)


def _alarmas(cloudwatch: Any, politica: Politica) -> dict[str, dict[str, Any]]:
    """Estado de la alarma alta y la baja: operador, umbral, estado y desde cuándo."""
    respuesta = cloudwatch.describe_alarms(AlarmNames=[politica.alarma_alta, politica.alarma_baja])
    por_nombre = {a["AlarmName"]: a for a in respuesta.get("MetricAlarms", [])}
    resultado = {}
    for clave, nombre in (("alta", politica.alarma_alta), ("baja", politica.alarma_baja)):
        alarma = por_nombre.get(nombre, {})
        resultado[clave] = {
            "operador": alarma.get("ComparisonOperator"),
            "umbral": alarma.get("Threshold"),
            "estado": alarma.get("StateValue"),
            "desde": _hora(alarma.get("StateUpdatedTimestamp")),
            "acciones": alarma.get("ActionsEnabled"),
        }
    return resultado


def _alarma(cloudwatch: Any, nombre: str) -> dict[str, Any]:
    """La alarma `nombre` tal como la devuelve `DescribeAlarms` (para `datos_de_estado`)."""
    alarmas = cloudwatch.describe_alarms(AlarmNames=[nombre]).get("MetricAlarms", [])
    if len(alarmas) != 1:
        pytest.fail(f"{len(alarmas)} alarmas {nombre!r} en DescribeAlarms")
    return alarmas[0]


def _historial(cloudwatch: Any, alarma: str, desde: datetime) -> list[tuple[str, str, str]]:
    """Cambios de estado y acciones de la alarma desde `desde` (¿invocó la política?)."""
    items = cloudwatch.describe_alarm_history(
        AlarmName=alarma, StartDate=desde, ScanBy="TimestampAscending", MaxRecords=50
    ).get("AlarmHistoryItems", [])
    return [
        (_hora(i.get("Timestamp")), i.get("HistoryItemType", ""), i.get("HistorySummary", ""))
        for i in items
    ]


def _actividades(autoscaling: Any, recurso: str, desde: datetime) -> list[dict[str, Any]]:
    actividades = autoscaling.describe_scaling_activities(
        ServiceNamespace="ecs", ResourceId=recurso, ScalableDimension=DIMENSION, MaxResults=50
    ).get("ScalingActivities", [])
    return [
        {
            "inicio": _hora(a.get("StartTime")),
            "fin": _hora(a.get("EndTime")) if a.get("EndTime") else None,
            "estado": a.get("StatusCode"),
            "descripcion": a.get("Description"),
        }
        for a in sorted(actividades, key=lambda a: a["StartTime"])
        if a["StartTime"] >= desde
    ]


def _retiradas(ecs: Any, cluster: str, desde: datetime) -> list[dict[str, Any]]:
    """Tareas de `cotizacion` detenidas desde `desde`, con el exitCode de cada contenedor."""
    arns = ecs.list_tasks(cluster=cluster, serviceName=SERVICIO, desiredStatus="STOPPED")[
        "taskArns"
    ]
    if not arns:
        return []
    tareas = ecs.describe_tasks(cluster=cluster, tasks=arns)["tasks"]
    return [
        {
            "id": t["taskArn"].rsplit("/", 1)[-1],
            "estado": t.get("lastStatus"),
            "detenida": _hora(t.get("stoppedAt")),
            "stopCode": t.get("stopCode"),
            "stoppedReason": t.get("stoppedReason"),
            "exitCode": {c.get("name"): c.get("exitCode") for c in t.get("containers", [])},
        }
        for t in tareas
        if (t.get("stoppingAt") or t.get("stoppedAt") or desde) >= desde
    ]


# --- La N2 -----------------------------------------------------------------------------------


@pytest.fixture
def guardas(request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    """Las guardas, en orden, antes de tocar AWS; y al final, el estado en que quedó."""
    if not seleccionada(request.config.getoption("keyword")):
        pytest.skip(MOTIVO_FILTRO)
    if os.getenv("ENV") != AMBIENTE:
        pytest.skip(f"HU-W08 N2: solo corre con ENV=int (ENV={os.getenv('ENV')!r})")
    cluster, prefijo = _cluster(request.getfixturevalue("ambiente").salidas)
    recurso = f"service/{cluster}/{SERVICIO}"
    aws, objetivos = _clientes_aws(recurso)

    nombre = f"{prefijo}-{SERVICIO}-cpu"
    if len(objetivos) != 1:
        _requerido(f"N2 {HISTORIA}: {recurso} no tiene scalable target ({len(objetivos)})")
    objetivo = objetivos[0]
    minimo, maximo = objetivo["MinCapacity"], objetivo["MaxCapacity"]
    suspendido = objetivo.get("SuspendedState", {})
    if not minimo < maximo:
        _requerido(
            f"N2 {HISTORIA}: {recurso} con min = {minimo} y max = {maximo}; hace falta min < max"
        )
    if suspendido.get("DynamicScalingOutSuspended"):
        _requerido(f"N2 {HISTORIA}: el escalado hacia fuera de {recurso} está suspendido")
    try:
        politica = politica_de(
            aws.autoscaling.describe_scaling_policies(
                ServiceNamespace="ecs",
                ResourceId=recurso,
                ScalableDimension=DIMENSION,
                PolicyNames=[nombre],
            ),
            nombre,
        )
    except LookupError:
        _requerido(f"N2 {HISTORIA}: no existe la política {nombre} en {recurso}")
    except ValueError as error:
        pytest.fail(f"N2 {HISTORIA}: no se reconocen las alarmas de {nombre}: {error}")
    servicio = _servicio(aws.ecs, cluster)
    if servicio["desiredCount"] != minimo:
        _requerido(
            f"N2 {HISTORIA}: {SERVICIO} tiene desiredCount = {servicio['desiredCount']} y min = "
            f"{minimo}; hace falta partir del mínimo (¿un escalado anterior aún no termina?)"
        )

    estado: dict[str, Any] = {
        "aws": aws,
        "cluster": cluster,
        "recurso": recurso,
        "politica": politica,
        "minimo": minimo,
        "maximo": maximo,
        "suspendido": suspendido,
        "forzada": None,
    }
    try:
        yield estado
    finally:
        if estado["forzada"] is not None:
            try:
                final = _alarmas(aws.cloudwatch, politica)
                servicio = _servicio(aws.ecs, cluster)
                aviso = (
                    "sigue en ALARM por la N2; vuelve sola en la siguiente evaluación (no se toca)"
                    if final["alta"]["estado"] == "ALARM"
                    else "ya no está en ALARM"
                )
                print(
                    f"\nHU-W08 N2, estado final: alarma alta {final['alta']['estado']} ({aviso}); "
                    f"alarma baja {final['baja']['estado']}; {SERVICIO} con desiredCount "
                    f"{servicio['desiredCount']} y runningCount {servicio['runningCount']} "
                    f"(min {minimo}, max {maximo}). El scalable target y la política no se tocaron."
                )
            except Exception as error:  # noqa: BLE001 - el informe final no oculta la falla
                print(f"\nHU-W08 N2: AVISO, no se pudo leer el estado final ({error!r})")


def test_hu_w08_n2_alarma_alta_escala_cotizacion(guardas: dict[str, Any]) -> None:
    aws: Aws = guardas["aws"]
    cluster, recurso = guardas["cluster"], guardas["recurso"]
    politica: Politica = guardas["politica"]
    minimo, maximo = guardas["minimo"], guardas["maximo"]

    antes = _alarmas(aws.cloudwatch, politica)
    if not str(antes["alta"]["operador"]).startswith("GreaterThan"):
        pytest.fail(f"la alarma {politica.alarma_alta} no es la alta: {antes['alta']}")

    # (a) Forzar la alarma alta: la única escritura de la prueba. Sin `StateReasonData`, App Auto
    # Scaling no ejecuta la política ("StateReasonData must be provided", N2 del 2026-11-20); con
    # las claves del ejemplo de SetAlarmState tampoco ("Metric data points must be provided", N2
    # del 2026-11-22): la plantilla es el StateReasonData que CloudWatch dejó en la alarma.
    alta = _alarma(aws.cloudwatch, politica.alarma_alta)
    plantilla = plantilla_de(alta)
    valor = valor_para_una_tarea_mas(float(alta["Threshold"]), minimo)
    hora, t0 = datetime.now(UTC), time.monotonic()
    enviado = datos_de_estado(alta, hora, valor)
    aws.cloudwatch.set_alarm_state(
        AlarmName=politica.alarma_alta,
        StateValue="ALARM",
        StateReason=RAZON_ALARMA,
        StateReasonData=enviado,
    )
    guardas["forzada"] = hora

    muestras: list[Muestra] = []
    _observar(
        aws.ecs,
        cluster,
        t0,
        ESPERA_SUBIDA_S,
        muestras,
        lambda m: m.deseadas > minimo and m.corriendo >= m.deseadas and m.pendientes == 0,
    )
    subida = next((m for m in muestras if m.deseadas > minimo), None)
    lista = next((m for m in muestras if m.deseadas > minimo and m.corriendo >= m.deseadas), None)

    # (b) Bajada, solo si subió.
    regreso = vuelta = None
    if subida is not None:
        _observar(
            aws.ecs,
            cluster,
            t0,
            ESPERA_BAJADA_S,
            muestras,
            lambda m: m.deseadas == minimo and m.corriendo == minimo,
        )
        despues = [m for m in muestras if m.t > subida.t]
        regreso = next((m for m in despues if m.deseadas == minimo), None)
        vuelta = next((m for m in despues if m.deseadas == m.corriendo == minimo), None)

    # (c) Evidencia.
    alarmas = _alarmas(aws.cloudwatch, politica)
    historial = _historial(aws.cloudwatch, politica.alarma_alta, hora)
    actividades = _actividades(aws.autoscaling, recurso, hora)
    retiradas = _retiradas(aws.ecs, cluster, hora)
    print(
        "\nHU-W08 N2, resumen para la evidencia:"
        f"\n- política {politica.nombre}: objetivo {politica.objetivo}, enfriamientos "
        f"{politica.enfriamiento_salida} s (salida) y {politica.enfriamiento_entrada} s (entrada); "
        f"MinCapacity {minimo}, MaxCapacity {maximo}, suspendido {guardas['suspendido']}"
        f"\n- alarma alta {politica.alarma_alta}; baja {politica.alarma_baja}"
        f"\n- alarmas antes: {antes}"
        f"\n- plantilla del StateReasonData (claves de la alarma alta, sin valores): "
        f"{claves_de(plantilla) or 'ninguna utilizable; formato por omisión'}"
        f"\n- StateReasonData enviado: {enviado}"
        f"\n- alarma alta forzada a ALARM a las {_hora(hora)}"
        f"\n- línea de tiempo (t s, deseadas, corriendo, pendientes): "
        f"{[(m.t, m.deseadas, m.corriendo, m.pendientes) for m in linea_de_tiempo(muestras)]}"
        f"\n- subida: deseadas > {minimo} a los {subida.t if subida else None} s; tareas "
        f"corriendo a los {lista.t if lista else None} s"
        f"\n- bajada: deseadas = {minimo} a los {regreso.t if regreso else None} s; corriendo = "
        f"{minimo} a los {vuelta.t if vuelta else None} s"
        f"{f' ({round(regreso.t - subida.t, 1)} s después de la subida)' if regreso and subida else ''}"
        f"\n- actividades de escalado: {actividades}"
        f"\n- historial de la alarma alta: {historial}"
        f"\n- alarmas después: {alarmas}"
        f"\n- tareas detenidas desde la alarma: {retiradas}"
    )

    # (a) AC1.
    if subida is None:
        pytest.fail(
            f"forzar la alarma alta no disparó el escalado: desiredCount siguió en {minimo} "
            f"durante {ESPERA_SUBIDA_S} s. Alarma alta: {alarmas['alta']['estado']} desde "
            f"{alarmas['alta']['desde']} "
            f"({'volvió sola' if alarmas['alta']['estado'] != 'ALARM' else 'sigue en ALARM'}); "
            f"historial {historial}; actividades {actividades}"
        )
    assert max(m.deseadas for m in muestras) <= maximo, (
        f"desiredCount pasó de MaxCapacity = {maximo}: {linea_de_tiempo(muestras)}"
    )
    # (b) AC2: nunca por debajo del mínimo.
    assert min(m.deseadas for m in muestras) >= minimo, (
        f"desiredCount bajó de MinCapacity = {minimo}: {linea_de_tiempo(muestras)}"
    )
    if regreso is None:
        print(
            f"\nHU-W08 N2: {SERVICIO} no volvió a {minimo} tareas en {ESPERA_BAJADA_S} s. No es una "
            "falla: el autoescalado lo devolverá solo tras el enfriamiento de entrada "
            f"({politica.enfriamiento_entrada} s) y la evaluación de la alarma baja."
        )
    else:
        # (c) AC3: la tarea retirada (se informa; el drenaje lo prueba HU-W07).
        print(f"\nHU-W08 N2: tarea retirada por el escalado hacia dentro: {retiradas}")


# --- Unitarios de las funciones puras (sin AWS) ----------------------------------------------


def _respuesta(*politicas: dict[str, Any]) -> dict[str, Any]:
    return {"ScalingPolicies": list(politicas)}


def _politica(nombre: str, alarmas: list[str] | None, tipo: str = "TargetTrackingScaling") -> dict:
    politica: dict[str, Any] = {
        "PolicyName": nombre,
        "PolicyType": tipo,
        "TargetTrackingScalingPolicyConfiguration": {
            "TargetValue": 60.0,
            "ScaleOutCooldown": 30,
            "ScaleInCooldown": 300,
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "ECSServiceAverageCPUUtilization"
            },
        },
    }
    if alarmas is not None:
        politica["Alarms"] = [
            {"AlarmName": a, "AlarmARN": f"arn:aws:cloudwatch:us-east-1:111:alarm:{a}"}
            for a in alarmas
        ]
    return politica


RECURSO = "TargetTracking-service/solventa-int/cotizacion"
ALTA = f"{RECURSO}-AlarmHigh-fc0e4183-23ac-497e-9992-691c9980c38e"
BAJA = f"{RECURSO}-AlarmLow-61a39305-ed6c-4ff8-b3f8-2b1c5e3ad1a2"


def test_hu_w08_seleccionada_solo_con_el_filtro() -> None:
    assert not seleccionada("")
    assert not seleccionada("hu_w07")
    assert seleccionada("hu_w08")
    assert seleccionada("hu_w07 or hu_w08")


def test_hu_w08_politica_de_elige_la_alta_y_la_baja_por_el_nombre_devuelto() -> None:
    otra = _politica(
        "solventa-int-clientes-cpu",
        [
            "TargetTracking-service/solventa-int/clientes-AlarmHigh-0aa",
            "TargetTracking-service/solventa-int/clientes-AlarmLow-0bb",
        ],
    )
    for alarmas in ([ALTA, BAJA], [BAJA, ALTA]):
        respuesta = _respuesta(otra, _politica("solventa-int-cotizacion-cpu", alarmas))
        assert politica_de(respuesta, "solventa-int-cotizacion-cpu") == Politica(
            "solventa-int-cotizacion-cpu", 60.0, 30, 300, ALTA, BAJA
        )


def test_hu_w08_politica_de_sin_la_politica() -> None:
    with pytest.raises(LookupError):
        politica_de({}, "solventa-int-cotizacion-cpu")
    with pytest.raises(LookupError):
        politica_de(_respuesta(_politica("solventa-int-clientes-cpu", [ALTA, BAJA])), "x")


@pytest.mark.parametrize(
    "alarmas",
    [
        None,
        [],
        [ALTA],
        [BAJA],
        [ALTA, ALTA.replace("fc0e", "aaaa"), BAJA],
        [ALTA, BAJA, BAJA.replace("61a3", "bbbb")],
        [f"{RECURSO}-AlarmHigh", f"{RECURSO}-AlarmLow"],
        [f"{RECURSO}-alarmhigh-fc0e", f"{RECURSO}-alarmlow-61a3"],
    ],
)
def test_hu_w08_politica_de_rechaza_alarmas_incompletas_o_ambiguas(
    alarmas: list[str] | None,
) -> None:
    respuesta = _respuesta(_politica("solventa-int-cotizacion-cpu", alarmas))
    with pytest.raises(ValueError):
        politica_de(respuesta, "solventa-int-cotizacion-cpu")


def test_hu_w08_politica_de_rechaza_lo_que_no_es_target_tracking() -> None:
    respuesta = _respuesta(_politica("solventa-int-cotizacion-cpu", [ALTA, BAJA], "StepScaling"))
    with pytest.raises(ValueError):
        politica_de(respuesta, "solventa-int-cotizacion-cpu")


def test_hu_w08_linea_de_tiempo_deja_los_cambios_y_la_ultima() -> None:
    muestras = [
        Muestra(0, 2, 2, 0),
        Muestra(10, 2, 2, 0),
        Muestra(20, 3, 2, 1),
        Muestra(30, 3, 2, 1),
        Muestra(40, 3, 3, 0),
        Muestra(50, 3, 3, 0),
    ]
    assert linea_de_tiempo(muestras) == [
        Muestra(0, 2, 2, 0),
        Muestra(20, 3, 2, 1),
        Muestra(40, 3, 3, 0),
        Muestra(50, 3, 3, 0),
    ]
    assert linea_de_tiempo([]) == []
    assert linea_de_tiempo([Muestra(0, 2, 2, 0)]) == [Muestra(0, 2, 2, 0)]


# La alarma alta tal como la devuelve `DescribeAlarms` (target tracking de CPU de ECS: el umbral es el
# objetivo de la política).
ALARMA_ALTA = {
    "AlarmName": ALTA,
    "ComparisonOperator": "GreaterThanThreshold",
    "Threshold": 60.0,
    "Statistic": "Average",
    "Period": 60,
    "EvaluationPeriods": 3,
    "MetricName": "CPUUtilization",
    "Namespace": "AWS/ECS",
}
AHORA = datetime(2026, 11, 20, 14, 32, 52, 31000, tzinfo=UTC)


def _capacidad(tareas: int, valor: float, objetivo: float, redondeo: Callable[[float], int]) -> int:
    """La capacidad que pediría target tracking con un ajuste proporcional al valor de la métrica."""
    return redondeo(tareas * valor / objetivo)


def test_hu_w08_valor_supera_el_umbral_y_pide_una_sola_tarea_mas() -> None:
    valor = valor_para_una_tarea_mas(60.0, 2)
    assert valor > 60.0
    # De 2 a 3 tareas sin llegar al máximo de INT (4), redondee hacia arriba o al más cercano.
    assert _capacidad(2, valor, 60.0, math.ceil) == 3
    assert _capacidad(2, valor, 60.0, round) == 3
    for tareas in (1, 2, 3, 5):
        for umbral in (50.0, 60.0, 70.0):
            valor = valor_para_una_tarea_mas(umbral, tareas)
            assert valor > umbral
            assert _capacidad(tareas, valor, umbral, math.ceil) == tareas + 1
            assert _capacidad(tareas, valor, umbral, round) == tareas + 1


def test_hu_w08_valor_para_una_tarea_mas_sin_tareas() -> None:
    with pytest.raises(ValueError):
        valor_para_una_tarea_mas(60.0, 0)


# El `StateReasonData` que CloudWatch escribe al evaluar la alarma alta (el que devuelve
# `DescribeAlarms`): camelCase minúscula y `evaluatedDatapoints`, como el ejemplo de la guía de
# CloudWatch "Alarm actions" (notificación de una alarma de una métrica). Los valores son de otra
# evaluación (alarma en OK, CPU baja) para ver que se reemplazan.
PLANTILLA = {
    "version": "1.0",
    "queryDate": "2026-11-22T09:10:41.502+0000",
    "startDate": "2026-11-22T09:07:00.000+0000",
    "statistic": "Average",
    "period": 60,
    "recentDatapoints": [3.1, 2.9, 3.4],
    "threshold": 60.0,
    "evaluatedDatapoints": [
        {"timestamp": "2026-11-22T09:09:00.000+0000", "sampleCount": 2.0, "value": 3.4},
        {"timestamp": "2026-11-22T09:08:00.000+0000", "sampleCount": 2.0, "value": 2.9},
        {"timestamp": "2026-11-22T09:07:00.000+0000", "sampleCount": 2.0, "value": 3.1},
    ],
}
# Sin `evaluatedDatapoints`, como el ejemplo "Single Metric Alarm" de "Alarm events and EventBridge".
PLANTILLA_SIN_EVALUADOS = {k: v for k, v in PLANTILLA.items() if k != "evaluatedDatapoints"}


def _con_plantilla(plantilla: Any, **cambios: Any) -> dict[str, Any]:
    texto = plantilla if isinstance(plantilla, str) else json.dumps(plantilla)
    return {**ALARMA_ALTA, "StateReasonData": texto, **cambios}


@pytest.mark.parametrize(
    "plantilla",
    [
        None,
        "",
        "no es JSON",
        '{"version": "1.0",',
        "[1, 2]",
        '"texto"',
        json.dumps({"version": "1.0", "queryDate": "2026-11-22T09:10:41.502+0000"}),
    ],
    ids=["falta", "vacia", "texto", "cortada", "lista", "cadena", "sin-recentDatapoints"],
)
def test_hu_w08_datos_de_estado_sin_plantilla_usa_el_formato_por_omision(
    plantilla: str | None,
) -> None:
    alarma = dict(ALARMA_ALTA) if plantilla is None else _con_plantilla(plantilla)
    assert plantilla_de(alarma) is None
    valor = valor_para_una_tarea_mas(60.0, 2)
    texto = datos_de_estado(alarma, AHORA, valor)
    assert isinstance(texto, str)
    assert len(texto) <= 4000
    datos = json.loads(texto)
    assert datos == {
        "version": "1.0",
        "queryDate": "2026-11-20T14:32:52.031+0000",
        "startDate": "2026-11-20T14:31:00.000+0000",
        "statistic": "Average",
        "period": 60,
        "recentDatapoints": [valor],
        "threshold": 60.0,
        "evaluatedDatapoints": [
            {"timestamp": "2026-11-20T14:31:00.000+0000", "sampleCount": 1.0, "value": valor}
        ],
    }
    assert list(datos) == list(PLANTILLA)
    assert datos["recentDatapoints"][0] > datos["threshold"]
    assert datos["evaluatedDatapoints"][0]["value"] > datos["threshold"]


def test_hu_w08_datos_de_estado_conserva_las_claves_de_la_plantilla() -> None:
    # Estadística, periodo y umbral distintos de los de la plantilla: salen de la alarma.
    alarma = _con_plantilla(PLANTILLA, Statistic="Maximum", Period=300, Threshold=45.5)
    texto = datos_de_estado(alarma, AHORA, 50.0)
    assert len(texto) <= 4000
    datos = json.loads(texto)
    assert list(datos) == list(PLANTILLA)
    assert datos == {
        "version": "1.0",
        "queryDate": "2026-11-20T14:32:52.031+0000",
        # El inicio es el periodo anterior al de la consulta, alineado al periodo (como en AWS).
        "startDate": "2026-11-20T14:25:00.000+0000",
        "statistic": "Maximum",
        "period": 300,
        "recentDatapoints": [50.0],
        "threshold": 45.5,
        "evaluatedDatapoints": [
            {"timestamp": "2026-11-20T14:25:00.000+0000", "sampleCount": 2.0, "value": 50.0}
        ],
    }
    assert list(datos["evaluatedDatapoints"][0]) == list(PLANTILLA["evaluatedDatapoints"][0])
    assert datos["recentDatapoints"][0] > datos["threshold"]
    assert datos["evaluatedDatapoints"][0]["value"] > datos["threshold"]
    otra_zona = AHORA.astimezone(timezone(timedelta(hours=-5)))
    assert json.loads(datos_de_estado(alarma, otra_zona, 80.0))["queryDate"] == (
        "2026-11-20T14:32:52.031+0000"
    )


def test_hu_w08_datos_de_estado_sin_evaluados_en_la_plantilla_no_los_agrega() -> None:
    valor = valor_para_una_tarea_mas(60.0, 2)
    datos = json.loads(datos_de_estado(_con_plantilla(PLANTILLA_SIN_EVALUADOS), AHORA, valor))
    assert list(datos) == list(PLANTILLA_SIN_EVALUADOS)
    assert datos["recentDatapoints"] == [valor]
    assert datos["startDate"] == "2026-11-20T14:31:00.000+0000"


def test_hu_w08_datos_de_estado_sampleCount_por_omision_y_claves_ajenas() -> None:
    plantilla = {
        **PLANTILLA,
        "evaluatedDatapoints": [{"timestamp": "2026-11-22T09:09:00.000+0000", "value": 3.4}],
        "otraClave": {"x": 1},
    }
    datos = json.loads(datos_de_estado(_con_plantilla(plantilla), AHORA, 80.0))
    assert list(datos) == list(plantilla)
    assert datos["otraClave"] == {"x": 1}  # lo que no se conoce se conserva tal cual
    assert datos["evaluatedDatapoints"] == [
        {"timestamp": "2026-11-20T14:31:00.000+0000", "value": 80.0, "sampleCount": 1.0}
    ]
    vacia = {**PLANTILLA, "evaluatedDatapoints": []}
    assert json.loads(datos_de_estado(_con_plantilla(vacia), AHORA, 80.0))[
        "evaluatedDatapoints"
    ] == [{"timestamp": "2026-11-20T14:31:00.000+0000", "sampleCount": 1.0, "value": 80.0}]


def test_hu_w08_datos_de_estado_rechaza_mas_de_4000_caracteres() -> None:
    alarma = _con_plantilla({**PLANTILLA, "relleno": "x" * 4000})
    with pytest.raises(ValueError):
        datos_de_estado(alarma, AHORA, 80.0)


def test_hu_w08_claves_de_la_plantilla_sin_valores() -> None:
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
    assert claves_de(PLANTILLA_SIN_EVALUADOS) == list(PLANTILLA_SIN_EVALUADOS)


@pytest.mark.parametrize("valor", [60.0, 59.9])
def test_hu_w08_datos_de_estado_rechaza_un_valor_que_no_supera_el_umbral(valor: float) -> None:
    with pytest.raises(ValueError):
        datos_de_estado(ALARMA_ALTA, AHORA, valor)
    with pytest.raises(ValueError):
        datos_de_estado(_con_plantilla(PLANTILLA), AHORA, valor)


@pytest.mark.parametrize("falta", ["Statistic", "Period", "Threshold"])
def test_hu_w08_datos_de_estado_rechaza_una_alarma_incompleta(falta: str) -> None:
    alarma = {k: v for k, v in ALARMA_ALTA.items() if k != falta}
    with pytest.raises(ValueError):
        datos_de_estado(alarma, AHORA, 80.0)


def test_hu_w08_datos_de_estado_rechaza_una_hora_sin_zona() -> None:
    with pytest.raises(ValueError):
        datos_de_estado(ALARMA_ALTA, AHORA.replace(tzinfo=None), 80.0)
