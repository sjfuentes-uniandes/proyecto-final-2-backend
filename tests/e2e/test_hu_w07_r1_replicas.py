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

Recorrido: usuario E2E del back-office (grupo `asesores`) y cliente C01, como HU-W03. N
cotizaciones idénticas, cada una con su Idempotency-Key y su X-Correlation-Id (`e2e-hu-w07-…`).
A mitad de la ráfaga, `ecs.stop_task` de una tarea con reason "HU-W07 N3". Comprueba:
(a) AC1, equivalencia: todas las 201 tienen la misma prima, el mismo desglose y la misma versión.
(b) 5xx dentro del presupuesto y solo en la ventana posterior al retiro.
(c) AC2, reemplazo: el servicio vuelve a sus tareas deseadas con una tarea nueva; la detenida
    tiene nuestro stoppedReason (otras detenidas, p. ej. por Spot, se informan aparte).
(d) Zonas antes y después.
(e) AC1, trazas: al menos 2 `solventa.task_id` distintos en X-Ray.

Las funciones puras del final (`task_ids_en`, `clasificar`, `seleccionada`) tienen sus unitarios en
este archivo y no tocan AWS.
"""

import json
import math
import os
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator
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
    retiro. Una falla cuenta en la ventana si terminó entre el retiro y retiro + ventana_s."""
    lista = list(resultados)
    por_codigo = Counter(str(r.estado) if r.estado is not None else "transporte" for r in lista)
    fallas = [r for r in lista if r.estado is None or r.estado >= 500]
    fuera = [r for r in fallas if not (retiro <= r.fin <= retiro + ventana_s)]
    return Clasificacion(
        dict(sorted(por_codigo.items())), fallas, fuera, math.floor(len(lista) * max_pct / 100)
    )


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

    def __init__(self, base: str, token: str, prefijo: str) -> None:
        self._url = f"{base}/web/v1/cotizaciones"
        self._cabeceras = {"Authorization": f"Bearer {token}"}
        self.prefijo = prefijo

    def cotizar(self, n: int) -> Resultado:
        correlacion = f"{self.prefijo}-{n:04d}"
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
            respuesta = httpx.post(self._url, json=cuerpo, headers=cabeceras, timeout=TIMEOUT_S)
        except httpx.HTTPError as error:
            return Resultado(
                correlacion, inicio, time.monotonic(), None, error=type(error).__name__
            )
        fin = time.monotonic()
        assert respuesta.status_code != 403, "el usuario E2E debe estar en el grupo asesores"
        try:
            datos = respuesta.json() if respuesta.status_code == 201 else None
        except ValueError:
            datos = None
        return Resultado(correlacion, inicio, fin, respuesta.status_code, datos)


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
    task_ids = _task_ids_en_xray(xray, [r.correlacion for r in resultados if r.estado == 201])

    print(
        "\nHU-W07 N3, resumen para la evidencia:"
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
