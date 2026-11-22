"""N3 R1 (HU-W07) en INT: una ráfaga de cotizaciones por el portal mientras se detiene una tarea.

**Detiene una tarea de `cotizacion` en INT.** Solo corre si la expresión `-k` de la sesión contiene
`hu_w07`; con el filtro vacío (todas las E2E) se omite siempre, también con `E2E_REQUERIDO=1`.
Configuración, precondiciones y cuándo es seguro lanzarla: infra/DESPLIEGUE.md (sección 7,
"Pruebas E2E (N3)").

Guardas, antes de tocar AWS:
1. `-k` con `hu_w07`.
2. `ENV=int`. Solo el clúster de ese ambiente (salida `platform.cluster.name` de infra/platform,
   `outputs.tf:13`; por convención `solventa-int`, `infra/platform/main.tf:34`) y el servicio
   `cotizacion` (el nombre del servicio de ECS es la clave del servicio, `infra/apps/ecs.tf:214`).
3. boto3 y credenciales (ECS y X-Ray), como HU-W05: sin ellas se omite, o falla con
   `E2E_REQUERIDO=1`.
4. Al menos 2 tareas RUNNING de `cotizacion` en zonas distintas (min = 2, HU-W07 paso 7.7).
5. Pase lo que pase, espera a que el servicio vuelva a sus tareas deseadas (tope configurable).

Recorrido: usuario E2E del back-office (grupo `asesores`) y cliente C01, como HU-W03.

Calentamiento (desde 2026-11-20): antes de la ráfaga, cotizaciones secuenciales (`<prefijo>-cal-NNNN`)
hasta `CALENTAR_OK` 201 seguidas, con `CALENTAR_MAX` intentos como tope; si se agota o hay un 4xx,
la prueba falla (no se omite). En INT, tras un despliegue y tras 39 min sin tráfico, las 2 primeras
solicitudes dieron 503 de bff-web (una por tarea, arranque en frío): la N3 mide el retiro, no el
frío, que se informa aparte (docs/hu-w07-replicas.md). Ninguna solicitud del calentamiento cuenta
para el presupuesto de 5xx ni para los task_id.

Ráfaga: N cotizaciones idénticas, cada una con su Idempotency-Key y su X-Correlation-Id
(`e2e-hu-w07-…`). A mitad de la ráfaga, `ecs.stop_task` de una tarea con reason "HU-W07 N3". Comprueba:
(a) AC1, equivalencia: todas las 201 tienen la misma prima, el mismo desglose y la misma versión.
(b) 5xx dentro del presupuesto y solo en la ventana posterior al retiro.
(c) AC2, reemplazo: el servicio vuelve a sus tareas deseadas con una tarea nueva; la detenida
    tiene nuestro stoppedReason (otras detenidas, p. ej. por Spot, se informan aparte).
(d) Zonas antes y después.
(e) AC1, trazas: al menos 2 `solventa.task_id` distintos en X-Ray.

Evidencia de quién respondió: de cada respuesta distinta de 201 (calentamiento o ráfaga) se guardan
el código, los primeros 200 caracteres del cuerpo y el X-Correlation-Id; el resumen agrega, de
X-Ray (solo lectura), cada servicio de su traza con su task_id y su tramo hoja más largo, o «sin
traza aún». Esa evidencia nunca hace fallar la prueba.

Las funciones puras (`task_ids_en`, `clasificar`, `seleccionada`, `decidir_calentamiento`,
`resumen_traza` y las de apoyo) tienen sus unitarios al final de este archivo y no tocan AWS.
"""

import json
import math
import os
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

HISTORIA = "HU-W07 (R1)"
FILTRO = "hu_w07"
MOTIVO_FILTRO = "HU-W07 N3: detiene una tarea de INT; selecciónala con -k hu_w07"
AMBIENTE = "int"
SERVICIO = "cotizacion"
RAZON_STOP = "HU-W07 N3"
TIMEOUT_S = 30
CLIENTE = os.getenv("E2E_W07_CLIENTE_ID", "00000000-0000-4000-8000-000000000001")
# Datos declarados sintéticos de C01 (contracts/datos-sinteticos), como HU-W03.
DATOS = {"fechaNacimiento": "1990-04-18", "plazoCreditoMeses": 180, "sumaAsegurada": 180000000}
# 160 solicitudes, una cada 0,5 s: 80 s de ráfaga, con el retiro a los 40 s. Son 160 en menos de
# 2 min, muy por debajo del WAF de INT (2000 por IP en 5 min).
SOLICITUDES = int(os.getenv("E2E_W07_SOLICITUDES", "160"))
INTERVALO_S = float(os.getenv("E2E_W07_INTERVALO_S", "0.5"))
CONCURRENCIA = int(os.getenv("E2E_W07_CONCURRENCIA", "8"))
MAX_5XX_PCT = float(os.getenv("E2E_W07_MAX_5XX_PCT", "1"))
VENTANA_5XX_S = float(os.getenv("E2E_W07_VENTANA_5XX_S", "30"))
ESPERA_REEMPLAZO_S = int(os.getenv("E2E_W07_ESPERA_REEMPLAZO_S", "300"))
ESPERA_TRAZA_S = int(os.getenv("E2E_W07_ESPERA_TRAZA_S", "120"))
CLAVES_TASK_ID = ("solventa.task_id", "solventa_task_id")  # X-Ray no admite '.' en anotaciones
# Calentamiento (2026-11-20): constantes y no variables de entorno, porque e2e.yml no las pasa.
# 4 seguidas: con 2 tareas y el reparto de Service Connect, casi siempre pasan por las dos.
CALENTAR_OK = 4
CALENTAR_MAX = 12
CALENTAR_INTERVALO_S = 0.5
LISTO, SEGUIR, AGOTADO, RECHAZO = "listo", "seguir", "agotado", "rechazo"
# Trazas de las respuestas distintas de 201: tope compartido de espera y texto si no hay.
ESPERA_TRAZA_FALLAS_S = 30
SIN_TRAZA = "sin traza aún"


# --- Funciones puras (con unitarios al final) --------------------------------------------------


def seleccionada(keyword: str) -> bool:
    """Si la sesión eligió esta prueba a propósito: `-k` con `hu_w07`."""
    return FILTRO in (keyword or "")


def task_ids_en(documento: Any) -> set[str]:
    """Los valores de `solventa.task_id` en un documento de X-Ray, en anotaciones o metadatos y a
    cualquier profundidad (subsegmentos incluidos). Tolera tipos inesperados."""
    vistos: set[str] = set()
    pendientes: list[Any] = [documento]
    while pendientes:
        actual = pendientes.pop()
        if isinstance(actual, dict):
            for clave, valor in actual.items():
                if clave in CLAVES_TASK_ID and isinstance(valor, str) and valor:
                    vistos.add(valor)
                elif isinstance(valor, (dict, list)):
                    pendientes.append(valor)
        elif isinstance(actual, list):
            pendientes.extend(actual)
    return vistos


@dataclass(frozen=True)
class Resultado:
    correlacion: str
    inicio: float  # time.monotonic() al enviar
    fin: float  # time.monotonic() al recibir (o fallar)
    estado: int | None  # None: error de transporte
    cuerpo: dict[str, Any] | None = None
    error: str = ""
    # Quién respondió, solo si no es 201: los primeros 200 caracteres del cuerpo y su
    # X-Correlation-Id (el de bff-web repite el nuestro; el del borde es su requestId).
    texto: str = ""
    correlacion_respuesta: str = ""
    calentamiento: bool = False  # nunca entra a clasificar ni a los task_id


@dataclass(frozen=True)
class Clasificacion:
    por_codigo: dict[str, int]
    fallas: list[Resultado]  # 5xx o errores de transporte
    fuera_de_ventana: list[Resultado]
    permitidas: int


def clasificar(
    resultados: Iterable[Resultado], retiro: float, ventana_s: float, max_pct: float
) -> Clasificacion:
    """Conteo por código y las fallas (5xx o transporte) dentro y fuera de la ventana posterior al
    retiro. Una falla cuenta en la ventana si terminó entre el retiro y retiro + ventana_s. Las
    solicitudes del calentamiento no cuentan."""
    lista = [r for r in resultados if not r.calentamiento]
    por_codigo = Counter(str(r.estado) if r.estado is not None else "transporte" for r in lista)
    fallas = [r for r in lista if r.estado is None or r.estado >= 500]
    fuera = [r for r in fallas if not (retiro <= r.fin <= retiro + ventana_s)]
    return Clasificacion(
        dict(sorted(por_codigo.items())), fallas, fuera, math.floor(len(lista) * max_pct / 100)
    )


def creadas_de_la_rafaga(resultados: Iterable[Resultado]) -> list[str]:
    """Las correlaciones 201 de la ráfaga, para buscar sus task_id (sin las del calentamiento)."""
    return [r.correlacion for r in resultados if r.estado == 201 and not r.calentamiento]


def decidir_calentamiento(estados: Sequence[int | None], consecutivos: int, maximo: int) -> str:
    """Qué sigue tras cada intento del calentamiento. `rechazo` si hubo un 4xx (no es frío: es la
    prueba o el borde); `listo` con `consecutivos` 201 seguidas al final (un 5xx, un error de
    transporte o cualquier otro código reinicia la racha); `agotado` al llegar a `maximo`
    intentos sin esa racha; si no, `seguir`."""
    racha = 0
    for estado in estados:
        if estado is not None and 400 <= estado < 500:
            return RECHAZO
        racha = racha + 1 if estado == 201 else 0
    if racha >= consecutivos:
        return LISTO
    if len(estados) >= maximo:
        return AGOTADO
    return SEGUIR


def _codigo(r: Resultado) -> str:
    return str(r.estado) if r.estado is not None else "transporte"


def resumen_calentamiento(decision: str, intentos: Sequence[Resultado]) -> str:
    """Para la evidencia: la decisión, el conteo por código y la latencia de cada intento."""
    por_codigo = dict(sorted(Counter(_codigo(r) for r in intentos).items()))
    latencias = [(r.correlacion, r.estado or r.error, round(r.fin - r.inicio, 2)) for r in intentos]
    return f"{decision} en {len(intentos)} intentos; por código {por_codigo}; latencias {latencias}"


def _numero(valor: Any) -> float:
    return float(valor) if isinstance(valor, (int, float)) else 0.0


def _duracion_ms(documento: dict[str, Any]) -> float | None:
    inicio, fin = documento.get("start_time"), documento.get("end_time")
    if isinstance(inicio, (int, float)) and isinstance(fin, (int, float)):
        return (fin - inicio) * 1000
    return None  # en curso o incompleto


def _hoja_mas_larga(subsegmentos: Any) -> tuple[str, float] | None:
    """El subsegmento sin hijos más largo, a cualquier profundidad: un envoltorio (la operación
    entera) no dice dónde se fue el tiempo; una hoja (una consulta, una llamada) sí."""
    mejor: tuple[str, float] | None = None
    pendientes = list(subsegmentos) if isinstance(subsegmentos, list) else []
    while pendientes:
        actual = pendientes.pop()
        if not isinstance(actual, dict):
            continue
        hijos = actual.get("subsegments")
        if isinstance(hijos, list) and hijos:
            pendientes.extend(hijos)
            continue
        duracion = _duracion_ms(actual)
        if duracion is not None and (mejor is None or duracion > mejor[1]):
            mejor = (str(actual.get("name") or "?"), duracion)
    return mejor


def resumen_traza(documentos: Iterable[Any]) -> list[str]:
    """Una línea por servicio (segmento) de la traza, en orden de inicio: nombre, código HTTP si lo
    hay, task_id si lo hay, duración y su tramo hoja más largo. Tolera documentos raros."""
    segmentos = [d for d in documentos if isinstance(d, dict)]
    segmentos.sort(key=lambda d: _numero(d.get("start_time")))
    lineas = []
    for segmento in segmentos:
        partes = [str(segmento.get("name") or "?")]
        http = segmento.get("http")
        respuesta = http.get("response") if isinstance(http, dict) else None
        estado = respuesta.get("status") if isinstance(respuesta, dict) else None
        if estado is not None:
            partes.append(str(estado))
        ids = sorted(task_ids_en(segmento))
        partes.append(f"task_id {','.join(ids)}" if ids else "sin task_id")
        duracion = _duracion_ms(segmento)
        partes.append(f"{duracion:.0f} ms" if duracion is not None else "? ms")
        hoja = _hoja_mas_larga(segmento.get("subsegments"))
        partes.append(f"tramo más largo: {hoja[0]} {hoja[1]:.0f} ms" if hoja else "sin tramos")
        lineas.append(", ".join(partes))
    return lineas or ["traza sin segmentos legibles"]


# --- Guardas ---------------------------------------------------------------------------------


def _requerido(motivo: str) -> None:
    """Falta de configuración o precondición: falla con E2E_REQUERIDO=1, si no se omite."""
    if os.getenv("E2E_REQUERIDO") == "1":
        pytest.fail(motivo)
    pytest.skip(motivo)


def _clientes_aws() -> tuple[Any, Any]:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover
        _requerido(f"N3 {HISTORIA}: boto3 no está instalado")
    region = os.getenv("AWS_REGION", "us-east-1")
    try:
        ecs, xray = (
            boto3.client("ecs", region_name=region),
            boto3.client("xray", region_name=region),
        )
        xray.get_sampling_rules()  # comprueba credenciales, como HU-W05
    except (BotoCoreError, ClientError) as error:
        _requerido(f"N3 {HISTORIA}: sin acceso a AWS ({type(error).__name__})")
    return ecs, xray


def _cluster(salidas: dict[str, Any]) -> str:
    plataforma = salidas.get("platform", {})
    if plataforma and plataforma.get("environment") != AMBIENTE:
        pytest.fail(
            f"las salidas de infra/platform son de {plataforma.get('environment')!r}, no de int"
        )
    nombre: str = plataforma.get("cluster", {}).get("name") or f"solventa-{AMBIENTE}"
    if nombre != f"solventa-{AMBIENTE}":
        pytest.fail(f"clúster inesperado para int: {nombre!r}")
    return nombre


# --- ECS -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tarea:
    arn: str
    zona: str
    estado: str
    razon: str = ""
    detenida: datetime | None = None

    @property
    def id(self) -> str:
        return self.arn.rsplit("/", 1)[-1]


def _tareas(ecs: Any, cluster: str, estado: str) -> list[Tarea]:
    arns = ecs.list_tasks(cluster=cluster, serviceName=SERVICIO, desiredStatus=estado)["taskArns"]
    if not arns:
        return []
    tareas = ecs.describe_tasks(cluster=cluster, tasks=arns)["tasks"]
    return sorted(
        (
            Tarea(
                t["taskArn"],
                t.get("availabilityZone", "?"),
                t.get("lastStatus", "?"),
                t.get("stoppedReason", ""),
                t.get("stoppedAt"),
            )
            for t in tareas
        ),
        key=lambda t: t.arn,
    )


def _servicio(ecs: Any, cluster: str) -> dict[str, Any]:
    (servicio,) = ecs.describe_services(cluster=cluster, services=[SERVICIO])["services"]
    return dict(servicio)


def _esperar_estable(ecs: Any, cluster: str, excluir: set[str], tope_s: int) -> tuple[bool, float]:
    """Hasta que runningCount == desiredCount, sin pendientes y con todas las RUNNING fuera de
    `excluir` (la detenida). Devuelve si lo logró y cuánto tardó."""
    inicio = time.monotonic()
    while True:
        servicio = _servicio(ecs, cluster)
        corriendo = [t for t in _tareas(ecs, cluster, "RUNNING") if t.estado == "RUNNING"]
        if (
            servicio["runningCount"] == servicio["desiredCount"]
            and servicio["pendingCount"] == 0
            and len(corriendo) == servicio["desiredCount"]
            and not {t.arn for t in corriendo} & excluir
        ):
            return True, time.monotonic() - inicio
        if time.monotonic() - inicio > tope_s:
            return False, time.monotonic() - inicio
        time.sleep(10)


# --- Portal y X-Ray --------------------------------------------------------------------------


class Portal:
    """El portal, por `/web`, con el usuario E2E del back-office (grupo `asesores`)."""

    def __init__(
        self, base: str, token: str, prefijo: str, cliente: httpx.Client | None = None
    ) -> None:
        self._url = f"{base}/web/v1/cotizaciones"
        self._cabeceras = {"Authorization": f"Bearer {token}"}
        self.prefijo = prefijo
        # Sin cliente (la N3), una conexión nueva por solicitud, como hasta ahora; los unitarios
        # pasan uno con un transporte falso.
        self._enviar = cliente.post if cliente is not None else httpx.post

    def cotizar(self, n: int, *, calentamiento: bool = False) -> Resultado:
        correlacion = f"{self.prefijo}-{'cal-' if calentamiento else ''}{n:04d}"
        cuerpo = {
            "clienteId": CLIENTE,
            "productoId": "vida-hipotecario",
            "datosAsegurado": DATOS,
            "coberturasOpcionales": ["ENFERMEDADES_GRAVES"],
        }
        cabeceras = {
            **self._cabeceras,
            "Idempotency-Key": str(uuid.uuid4()),
            "X-Correlation-Id": correlacion,
        }
        inicio = time.monotonic()
        try:
            respuesta = self._enviar(self._url, json=cuerpo, headers=cabeceras, timeout=TIMEOUT_S)
        except httpx.HTTPError as error:
            return Resultado(
                correlacion,
                inicio,
                time.monotonic(),
                None,
                error=type(error).__name__,
                calentamiento=calentamiento,
            )
        fin = time.monotonic()
        assert respuesta.status_code != 403, "el usuario E2E debe estar en el grupo asesores"
        if respuesta.status_code != 201:
            return Resultado(
                correlacion,
                inicio,
                fin,
                respuesta.status_code,
                texto=respuesta.text[:200],
                correlacion_respuesta=respuesta.headers.get("X-Correlation-Id", ""),
                calentamiento=calentamiento,
            )
        try:
            datos = respuesta.json()
        except ValueError:
            datos = None
        return Resultado(correlacion, inicio, fin, 201, datos, calentamiento=calentamiento)


def _calentar(
    portal: Portal,
    consecutivos: int = CALENTAR_OK,
    maximo: int = CALENTAR_MAX,
    intervalo_s: float = CALENTAR_INTERVALO_S,
    dormir: Callable[[float], Any] = time.sleep,
) -> tuple[str, list[Resultado]]:
    """Intentos secuenciales hasta que `decidir_calentamiento` deje de decir `seguir`, con
    `intervalo_s` entre uno y otro. Cada intento lleva su Idempotency-Key (la pone `cotizar`)."""
    intentos: list[Resultado] = []
    while True:
        intentos.append(portal.cotizar(len(intentos), calentamiento=True))
        decision = decidir_calentamiento([r.estado for r in intentos], consecutivos, maximo)
        if decision != SEGUIR:
            return decision, intentos
        dormir(intervalo_s)


def _rafaga(
    portal: Portal, ecs: Any, cluster: str, objetivo: Tarea, guardas: dict[str, Any]
) -> tuple[list[Resultado], float, datetime]:
    """Las solicitudes a su ritmo y, a mitad, el stop de la tarea. Devuelve el instante del stop
    (monotónico y de reloj). La tarea queda anotada en `guardas` solo si el stop se aceptó."""
    inicio = time.monotonic()

    def en_su_turno(n: int) -> Resultado:
        espera = inicio + n * INTERVALO_S - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        return portal.cotizar(n)

    with ThreadPoolExecutor(max_workers=CONCURRENCIA) as hilos:
        futuros = [hilos.submit(en_su_turno, n) for n in range(SOLICITUDES)]
        espera = inicio + (SOLICITUDES // 2) * INTERVALO_S - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        retiro, hora_stop = time.monotonic(), datetime.now(UTC)
        ecs.stop_task(cluster=cluster, task=objetivo.arn, reason=RAZON_STOP)
        guardas["detenida"] = objetivo
        resultados = [f.result() for f in futuros]
    return resultados, retiro, hora_stop


def _task_ids_en_xray(xray: Any, correlaciones: list[str]) -> set[str]:
    """Recorre las correlaciones de la ráfaga hasta ver 2 task_id distintos (o agotarlas)."""
    vistos: set[str] = set()
    limite = time.monotonic() + ESPERA_TRAZA_S
    for correlacion in correlaciones:
        filtro = f'annotation.correlation_id = "{correlacion}"'
        while True:
            ahora = datetime.now(UTC)
            ids = [
                r["Id"]
                for r in xray.get_trace_summaries(
                    StartTime=ahora - timedelta(minutes=30), EndTime=ahora, FilterExpression=filtro
                )["TraceSummaries"]
            ]
            if ids or time.monotonic() > limite:
                break
            time.sleep(5)
        for inicio in range(0, len(ids), 5):
            for traza in xray.batch_get_traces(TraceIds=ids[inicio : inicio + 5])["Traces"]:
                for segmento in traza["Segments"]:
                    vistos |= task_ids_en(json.loads(segmento["Document"]))
        if len(vistos) >= 2:
            break
        time.sleep(0.3)  # GetTraceSummaries: pocas solicitudes por segundo
    return vistos


def _documentos_xray(xray: Any, correlacion: str) -> list[Any]:
    ahora = datetime.now(UTC)
    filtro = f'annotation.correlation_id = "{correlacion}"'
    ids = [
        r["Id"]
        for r in xray.get_trace_summaries(
            StartTime=ahora - timedelta(minutes=30), EndTime=ahora, FilterExpression=filtro
        )["TraceSummaries"]
    ]
    documentos = []
    for inicio in range(0, len(ids), 5):
        for traza in xray.batch_get_traces(TraceIds=ids[inicio : inicio + 5])["Traces"]:
            documentos += [json.loads(s["Document"]) for s in traza["Segments"]]
    return documentos


def _trazas_de_fallas(
    xray: Any,
    correlaciones: Iterable[str],
    tope_s: float = ESPERA_TRAZA_FALLAS_S,
    dormir: Callable[[float], Any] = time.sleep,
) -> dict[str, list[str]]:
    """Solo lectura y solo evidencia: por correlación, `resumen_traza` de su traza, o `SIN_TRAZA`
    si X-Ray no la tiene dentro de `tope_s` (compartido), o el error de X-Ray. Nunca falla."""
    orden = list(dict.fromkeys(correlaciones))
    detalle: dict[str, list[str]] = {}
    pendientes = list(orden)
    limite = time.monotonic() + tope_s
    while pendientes:
        for correlacion in list(pendientes):
            try:
                documentos = _documentos_xray(xray, correlacion)
            except Exception as error:  # noqa: BLE001 - la evidencia nunca hace fallar la N3
                detalle[correlacion] = [f"X-Ray no respondió ({type(error).__name__})"]
                pendientes.remove(correlacion)
                continue
            if documentos:
                detalle[correlacion] = resumen_traza(documentos)
                pendientes.remove(correlacion)
            dormir(0.3)  # GetTraceSummaries: pocas solicitudes por segundo
        if not pendientes or time.monotonic() >= limite:
            break
        dormir(5)
    for correlacion in pendientes:
        detalle[correlacion] = [SIN_TRAZA]
    return {c: detalle[c] for c in orden}


def _evidencia_no_201(xray: Any, resultados: Sequence[Resultado]) -> str:
    """Las respuestas distintas de 201 (código, X-Correlation-Id y cuerpo) y sus trazas."""
    no_201 = [r for r in resultados if r.estado != 201]
    if not no_201:
        return "\n- respuestas distintas de 201: ninguna"
    trazas = _trazas_de_fallas(xray, [r.correlacion for r in no_201])
    lineas = [f"\n- respuestas distintas de 201: {len(no_201)}"]
    for r in no_201:
        lineas.append(
            f"\n  - {r.correlacion}: {r.estado or r.error}, X-Correlation-Id "
            f"{r.correlacion_respuesta or '-'!r}, cuerpo {r.texto!r}"
            f"\n    X-Ray: {' | '.join(trazas[r.correlacion])}"
        )
    return "".join(lineas)


# --- La N3 -----------------------------------------------------------------------------------


@pytest.fixture
def guardas(request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    """Las guardas, en orden, antes de tocar AWS; y al final, el servicio como estaba."""
    if not seleccionada(request.config.getoption("keyword")):
        pytest.skip(MOTIVO_FILTRO)
    if os.getenv("ENV") != AMBIENTE:
        pytest.skip(f"HU-W07 N3: solo corre con ENV=int (ENV={os.getenv('ENV')!r})")
    ambiente = request.getfixturevalue("ambiente")
    cluster = _cluster(ambiente.salidas)
    config = ambiente.requerir(
        HISTORIA, {"canales": ambiente.urls["canales"], "admin_token": ambiente.token_admin()}
    )
    ecs, xray = _clientes_aws()
    antes = [t for t in _tareas(ecs, cluster, "RUNNING") if t.estado == "RUNNING"]
    if len(antes) < 2 or len({t.zona for t in antes}) < 2:
        _requerido(
            f"N3 {HISTORIA}: min = 2 aún no aplicado; {SERVICIO} en {cluster} tiene "
            f"{[(t.id, t.zona) for t in antes]} (hacen falta 2 RUNNING en zonas distintas)"
        )
    estado: dict[str, Any] = {
        "ecs": ecs,
        "xray": xray,
        "cluster": cluster,
        "antes": antes,
        "portal": Portal(
            config["canales"], config["admin_token"], f"e2e-hu-w07-{uuid.uuid4().hex[:8]}"
        ),
        "detenida": None,
    }
    try:
        yield estado
    finally:
        excluir = {estado["detenida"].arn} if estado["detenida"] else set()
        logrado, segundos = _esperar_estable(ecs, cluster, excluir, ESPERA_REEMPLAZO_S)
        estado["reemplazo"] = (logrado, segundos)
        if not logrado:
            print(
                f"\nHU-W07 N3: AVISO, {SERVICIO} no volvió a sus tareas deseadas en "
                f"{ESPERA_REEMPLAZO_S} s; revisar el servicio en {cluster}"
            )


def test_hu_w07_r1_rafaga_con_retiro_de_una_tarea(guardas: dict[str, Any]) -> None:
    ecs, xray, cluster = guardas["ecs"], guardas["xray"], guardas["cluster"]
    antes: list[Tarea] = guardas["antes"]
    # Calentamiento, antes de tocar ninguna tarea: si no se logra, la prueba falla con su evidencia.
    decision, calentamiento = _calentar(guardas["portal"])
    evidencia_calentamiento = f"\n- calentamiento: {resumen_calentamiento(decision, calentamiento)}"
    if decision != LISTO:
        pytest.fail(
            f"HU-W07 N3: calentamiento {decision} (sin {CALENTAR_OK} respuestas 201 seguidas en "
            f"{CALENTAR_MAX} intentos, o un 4xx); no se detuvo ninguna tarea"
            f"{evidencia_calentamiento}{_evidencia_no_201(xray, calentamiento)}"
        )
    # (d) Antes de detener: las tareas están en zonas distintas (guarda 4).
    objetivo = antes[0]
    hora_inicio = datetime.now(UTC)
    resultados, retiro, hora_stop = _rafaga(guardas["portal"], ecs, cluster, objetivo, guardas)

    # (c) AC2: reemplazo con una tarea nueva y nuestro stoppedReason.
    logrado, segundos = _esperar_estable(ecs, cluster, {objetivo.arn}, ESPERA_REEMPLAZO_S)
    despues = [t for t in _tareas(ecs, cluster, "RUNNING") if t.estado == "RUNNING"]
    detenidas = [
        t for t in _tareas(ecs, cluster, "STOPPED") if t.detenida and t.detenida >= hora_inicio
    ]
    nuestra = [t for t in detenidas if t.arn == objetivo.arn]
    otras = [t for t in detenidas if t.arn != objetivo.arn]

    # (b) 5xx.
    clasificacion = clasificar(resultados, retiro, VENTANA_5XX_S, MAX_5XX_PCT)
    creadas = [r.cuerpo for r in resultados if r.estado == 201 and r.cuerpo]

    # (e) Trazas.
    task_ids = _task_ids_en_xray(xray, creadas_de_la_rafaga(resultados))

    print(
        "\nHU-W07 N3, resumen para la evidencia:"
        f"{evidencia_calentamiento}"
        f"\n- antes: {[(t.id, t.zona) for t in antes]}"
        f"\n- detenida: {objetivo.id} ({objetivo.zona}) a las {hora_stop.isoformat()}, "
        f"stoppedReason {[t.razon for t in nuestra]}"
        f"\n- otras detenidas en la ventana: {[(t.id, t.zona, t.razon) for t in otras]}"
        f"\n- después: {[(t.id, t.zona) for t in despues]}"
        f"\n- reemplazo: {'listo' if logrado else 'NO listo'} en {segundos:.0f} s"
        f"\n- ráfaga: {len(resultados)} solicitudes cada {INTERVALO_S} s; por código "
        f"{clasificacion.por_codigo}"
        f"\n- fallas: {len(clasificacion.fallas)} (permitidas {clasificacion.permitidas}); "
        f"fuera de la ventana de {VENTANA_5XX_S:.0f} s: {len(clasificacion.fuera_de_ventana)}; "
        f"{[(r.correlacion, r.estado or r.error, round(r.fin - retiro, 1)) for r in clasificacion.fallas]}"
        f"\n- task_id vistos en X-Ray: {sorted(task_ids)}"
        f"{_evidencia_no_201(xray, calentamiento + resultados)}"
    )

    # (a) AC1, equivalencia.
    assert creadas, "ninguna cotización 201 en la ráfaga"
    for campo in ("primaTotal", "desglose", "versionReglas"):
        valores = {json.dumps(c[campo], sort_keys=True) for c in creadas}
        assert len(valores) == 1, (campo, valores)
    # Ningún 4xx: un rechazo funcional o del borde no es parte del retiro.
    cuatro = {k: v for k, v in clasificacion.por_codigo.items() if k.startswith("4")}
    assert not cuatro, cuatro
    # (b) 5xx dentro del presupuesto y solo en la ventana.
    assert len(clasificacion.fallas) <= clasificacion.permitidas, clasificacion.fallas
    assert not clasificacion.fuera_de_ventana, clasificacion.fuera_de_ventana
    # (c) AC2.
    assert logrado, f"{SERVICIO} no volvió a sus tareas deseadas en {ESPERA_REEMPLAZO_S} s"
    assert nuestra and nuestra[0].razon == RAZON_STOP, nuestra
    assert objetivo.arn not in {t.arn for t in despues}
    assert {t.arn for t in despues} - {t.arn for t in antes}, "sin tarea nueva"
    # (e) AC1, trazas de réplicas distintas.
    assert len(task_ids) >= 2, f"menos de 2 solventa.task_id en X-Ray: {task_ids}"


# --- Unitarios de las funciones puras (sin AWS) ----------------------------------------------


def test_hu_w07_seleccionada_solo_con_el_filtro() -> None:
    assert not seleccionada("")
    assert not seleccionada("hu_w03 or hu_w05")
    assert seleccionada("hu_w07")
    assert seleccionada("hu_w07 and rafaga")


def test_hu_w07_task_ids_en_metadatos_y_anotaciones_a_cualquier_profundidad() -> None:
    documento = {
        "name": "bff-web",
        "annotations": {"correlation_id": "e2e-hu-w07-x-0001"},
        "subsegments": [
            {
                "name": "cotizacion",
                "metadata": {"default": {"solventa.task_id": "aaa111", "operation": "cotizacion"}},
                "subsegments": [{"name": "otro", "annotations": {"solventa_task_id": "bbb222"}}],
            },
            {"name": "sin", "metadata": {"default": {"solventa.presupuesto_ms": 700}}},
        ],
    }
    assert task_ids_en(documento) == {"aaa111", "bbb222"}
    assert task_ids_en([documento, {"metadata": {"x": {"y": [{"solventa.task_id": "ccc"}]}}}]) == {
        "aaa111",
        "bbb222",
        "ccc",
    }


def test_hu_w07_task_ids_tolera_documentos_raros() -> None:
    assert task_ids_en({}) == set()
    assert task_ids_en(None) == set()
    assert task_ids_en("texto") == set()
    assert task_ids_en({"metadata": {"default": {"solventa.task_id": ""}}}) == set()
    assert task_ids_en({"metadata": {"default": {"solventa.task_id": 123}}}) == set()
    assert task_ids_en({"annotations": {"task_id": "no-es-la-clave"}}) == set()


def test_hu_w07_clasificar_cuenta_y_separa_por_ventana() -> None:
    def r(n: int, fin: float, estado: int | None) -> Resultado:
        return Resultado(f"c-{n}", fin - 0.2, fin, estado)

    resultados = [r(i, 10 + i, 201) for i in range(196)] + [
        r(900, 101, 502),  # 1 s después del retiro (100)
        r(901, 129, 503),  # dentro de los 30 s
        r(902, 131, 500),  # fuera
        r(903, 99, None),  # transporte, antes del retiro
    ]
    clasificacion = clasificar(resultados, retiro=100, ventana_s=30, max_pct=1)
    assert clasificacion.por_codigo == {"201": 196, "500": 1, "502": 1, "503": 1, "transporte": 1}
    assert [x.correlacion for x in clasificacion.fallas] == ["c-900", "c-901", "c-902", "c-903"]
    assert [x.correlacion for x in clasificacion.fuera_de_ventana] == ["c-902", "c-903"]
    assert clasificacion.permitidas == 2  # 1 % de 200


# --- Unitarios del calentamiento y de la evidencia (2026-11-20; sin AWS) ---------------------


def test_hu_w07_calentamiento_listo_con_racha_de_201_al_final() -> None:
    assert decidir_calentamiento([503, 503, 201, 201, 201, 201], 4, 12) == LISTO
    assert decidir_calentamiento([503, 503, 201, 201, 201], 4, 12) == SEGUIR
    assert decidir_calentamiento([], 4, 12) == SEGUIR
    # La racha completa en el último intento permitido gana al agotamiento.
    assert decidir_calentamiento([503] * 8 + [201] * 4, 4, 12) == LISTO


def test_hu_w07_calentamiento_un_5xx_o_un_transporte_reinicia_la_racha() -> None:
    assert decidir_calentamiento([201, 503, 201, 201, 201], 4, 12) == SEGUIR
    assert decidir_calentamiento([201, None, 201, 201, 201], 4, 12) == SEGUIR
    assert decidir_calentamiento([201, 200, 201, 201, 201], 4, 12) == SEGUIR


def test_hu_w07_calentamiento_agotado_al_llegar_al_maximo() -> None:
    assert decidir_calentamiento([503] * 11, 4, 12) == SEGUIR
    assert decidir_calentamiento([503] * 12, 4, 12) == AGOTADO
    assert decidir_calentamiento([None] * 12, 4, 12) == AGOTADO


def test_hu_w07_calentamiento_un_4xx_es_rechazo_inmediato() -> None:
    assert decidir_calentamiento([503, 401], 4, 12) == RECHAZO
    assert decidir_calentamiento([201, 201, 201, 422], 4, 12) == RECHAZO


class _PortalFalso:
    """Devuelve los estados dados en orden y anota cada llamada (n, calentamiento)."""

    def __init__(self, estados: list[int | None]) -> None:
        self._estados = estados
        self.llamadas: list[tuple[int, bool]] = []

    def cotizar(self, n: int, *, calentamiento: bool = False) -> Resultado:
        self.llamadas.append((n, calentamiento))
        sufijo = "cal-" if calentamiento else ""
        return Resultado(
            f"p-{sufijo}{n:04d}", float(n), n + 0.25, self._estados[n], calentamiento=calentamiento
        )


def test_hu_w07_calentar_secuencial_hasta_listo_con_su_pausa() -> None:
    portal = _PortalFalso([503, 503, 201, 201, 201, 201, 201, 201])
    pausas: list[float] = []
    decision, intentos = _calentar(portal, dormir=pausas.append)
    assert decision == LISTO
    assert [r.correlacion for r in intentos] == [f"p-cal-{n:04d}" for n in range(6)]
    assert portal.llamadas == [(n, True) for n in range(6)]
    assert all(r.calentamiento for r in intentos)
    assert pausas == [CALENTAR_INTERVALO_S] * 5  # entre intentos, no después del último
    assert (CALENTAR_OK, CALENTAR_MAX, CALENTAR_INTERVALO_S) == (4, 12, 0.5)


def test_hu_w07_calentar_agotado_y_rechazo() -> None:
    decision, intentos = _calentar(_PortalFalso([503] * 20), dormir=lambda _: None)
    assert (decision, len(intentos)) == (AGOTADO, CALENTAR_MAX)
    decision, intentos = _calentar(_PortalFalso([503, 401, 201]), dormir=lambda _: None)
    assert (decision, len(intentos)) == (RECHAZO, 2)


def test_hu_w07_el_calentamiento_no_entra_a_clasificar_ni_a_task_ids() -> None:
    rafaga = [Resultado(f"c-{i}", i, i + 0.2, 201, {"x": 1}) for i in range(100)]
    calentamiento = [
        Resultado("c-cal-0000", 0, 2, 503, calentamiento=True),
        Resultado("c-cal-0001", 0, 2, 201, {"x": 1}, calentamiento=True),
    ]
    clasificacion = clasificar(calentamiento + rafaga, retiro=50, ventana_s=30, max_pct=1)
    assert clasificacion.por_codigo == {"201": 100}
    assert clasificacion.fallas == []
    assert clasificacion.permitidas == 1
    assert creadas_de_la_rafaga(calentamiento + rafaga) == [f"c-{i}" for i in range(100)]


def test_hu_w07_resumen_del_calentamiento_por_codigo_y_latencia() -> None:
    intentos = [
        Resultado("p-cal-0000", 10.0, 12.04, 503, calentamiento=True),
        Resultado("p-cal-0001", 12.5, 12.9, None, error="ReadTimeout", calentamiento=True),
        Resultado("p-cal-0002", 13.4, 13.7, 201, calentamiento=True),
    ]
    texto = resumen_calentamiento(AGOTADO, intentos)
    assert texto.startswith("agotado en 3 intentos")
    assert "{'201': 1, '503': 1, 'transporte': 1}" in texto
    assert "('p-cal-0000', 503, 2.04)" in texto
    assert "('p-cal-0001', 'ReadTimeout', 0.4)" in texto


def _portal_con(responder: Any) -> Portal:
    cliente = httpx.Client(transport=httpx.MockTransport(responder))
    return Portal("https://canales.example", "token-falso", "e2e-hu-w07-test", cliente=cliente)


def test_hu_w07_portal_guarda_quien_respondio_si_no_es_201() -> None:
    vistas: list[httpx.Request] = []
    cuerpo = '{"codigo":"SERVICIO_NO_DISPONIBLE","mensaje":"x","correlationId":"c","detalles":{}}'

    def responder(solicitud: httpx.Request) -> httpx.Response:
        vistas.append(solicitud)
        eco = {"X-Correlation-Id": solicitud.headers["X-Correlation-Id"]}
        return httpx.Response(503, text=cuerpo + " " * 300, headers=eco)

    portal = _portal_con(responder)
    resultado = portal.cotizar(3, calentamiento=True)
    assert resultado.correlacion == "e2e-hu-w07-test-cal-0003"
    assert (resultado.estado, resultado.calentamiento, resultado.cuerpo) == (503, True, None)
    assert resultado.correlacion_respuesta == "e2e-hu-w07-test-cal-0003"
    assert resultado.texto.startswith(cuerpo) and len(resultado.texto) == 200
    assert portal.cotizar(4).correlacion == "e2e-hu-w07-test-0004"
    claves = {v.headers["Idempotency-Key"] for v in vistas}
    assert len(claves) == 2  # una por intento


def test_hu_w07_portal_sin_cuerpo_ni_cabecera_y_201_sin_texto() -> None:
    estados = iter([503, 201])

    def responder(_: httpx.Request) -> httpx.Response:
        estado = next(estados)
        if estado == 201:
            return httpx.Response(201, json={"primaTotal": "1"}, headers={"X-Correlation-Id": "z"})
        return httpx.Response(estado)

    portal = _portal_con(responder)
    vacio = portal.cotizar(0)
    assert (vacio.estado, vacio.texto, vacio.correlacion_respuesta) == (503, "", "")
    creada = portal.cotizar(1)
    assert (creada.estado, creada.cuerpo, creada.texto) == (201, {"primaTotal": "1"}, "")


class _XRayFalso:
    """get_trace_summaries por correlación (o un error) y batch_get_traces por Id."""

    def __init__(
        self,
        ids: dict[str, list[str]] | None = None,
        documentos: dict[str, list[dict[str, Any]]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._ids, self._documentos, self._error = ids or {}, documentos or {}, error
        self.consultas = 0

    def get_trace_summaries(self, **kwargs: Any) -> dict[str, Any]:
        self.consultas += 1
        if self._error:
            raise self._error
        correlacion = kwargs["FilterExpression"].split('"')[1]
        return {"TraceSummaries": [{"Id": i} for i in self._ids.get(correlacion, [])]}

    def batch_get_traces(self, TraceIds: list[str]) -> dict[str, Any]:
        return {
            "Traces": [
                {"Id": i, "Segments": [{"Document": json.dumps(d)} for d in self._documentos[i]]}
                for i in TraceIds
            ]
        }


def test_hu_w07_trazas_de_fallas_sin_traza_aun_no_falla() -> None:
    xray = _XRayFalso()
    detalle = _trazas_de_fallas(xray, ["c-1", "c-2", "c-1"], tope_s=0, dormir=lambda _: None)
    assert detalle == {"c-1": [SIN_TRAZA], "c-2": [SIN_TRAZA]}
    assert SIN_TRAZA == "sin traza aún"
    assert xray.consultas == 2


def test_hu_w07_trazas_de_fallas_con_error_de_xray_no_falla() -> None:
    xray = _XRayFalso(error=RuntimeError("Throttling"))
    detalle = _trazas_de_fallas(xray, ["c-1"], tope_s=0, dormir=lambda _: None)
    assert detalle == {"c-1": ["X-Ray no respondió (RuntimeError)"]}


def test_hu_w07_trazas_de_fallas_resume_la_traza_encontrada() -> None:
    documentos = {
        "t-1": [
            {
                "name": "cotizacion",
                "start_time": 100.2,
                "end_time": 101.9,
                "http": {"response": {"status": 201}},
                "metadata": {"default": {"solventa.task_id": "aaa111"}},
                "subsegments": [{"name": "SELECT", "start_time": 100.25, "end_time": 101.85}],
            },
            {
                "name": "bff-web",
                "start_time": 100.0,
                "end_time": 101.6,
                "http": {"response": {"status": 503}},
                "subsegments": [{"name": "POST", "start_time": 100.05, "end_time": 101.55}],
            },
        ]
    }
    xray = _XRayFalso(ids={"c-1": ["t-1"]}, documentos=documentos)
    detalle = _trazas_de_fallas(xray, ["c-1"], tope_s=0, dormir=lambda _: None)
    assert detalle == {
        "c-1": [
            "bff-web, 503, sin task_id, 1600 ms, tramo más largo: POST 1500 ms",
            "cotizacion, 201, task_id aaa111, 1700 ms, tramo más largo: SELECT 1600 ms",
        ]
    }


def test_hu_w07_resumen_traza_toma_la_hoja_mas_larga_y_tolera_documentos_raros() -> None:
    segmento = {
        "name": "cotizacion",
        "start_time": 10.0,
        "end_time": 11.0,
        "subsegments": [
            {
                "name": "cotizacion",  # envoltorio de la operación: no es un tramo hoja
                "start_time": 10.0,
                "end_time": 10.95,
                "subsegments": [
                    {"name": "connect", "start_time": 10.0, "end_time": 10.8},
                    {"name": "clientes", "start_time": 10.8, "end_time": 10.85},
                ],
            },
            {"name": "abierto", "start_time": 10.9, "in_progress": True},
        ],
    }
    assert resumen_traza([segmento]) == [
        "cotizacion, sin task_id, 1000 ms, tramo más largo: connect 800 ms"
    ]
    assert resumen_traza([{"name": "x", "in_progress": True}]) == [
        "x, sin task_id, ? ms, sin tramos"
    ]
    assert resumen_traza([]) == ["traza sin segmentos legibles"]
    assert resumen_traza(["texto", None]) == ["traza sin segmentos legibles"]
