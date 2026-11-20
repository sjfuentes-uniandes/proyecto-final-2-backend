"""N3 de HU-W09: operación del experimento de carga de la cotización en EXP (ARQ-002 y ARQ-003).

**Lanzan tareas Fargate de k6 y mueven una API key de plan de uso en EXP.** Cada una de las cuatro
pruebas corre solo si la expresión `-k` de la sesión contiene su propio nombre: con el filtro vacío
(todas las E2E) o con `-k hu_w09`, se omiten siempre, también con `E2E_REQUERIDO=1`. El protocolo
está en docs/hu-w09-experimento.md y en docs/plan-experimento-hu-w09.md §f; la configuración, en
infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)"). No usan el portal ni el usuario E2E.

Orden: preparar → lanzar → esperar al menos 40 min → recoger → destruir EXP (detener, si hace falta).

- `hu_w09_preparar`: la cadena estable; el estado de la suscripción de alertas (solo se informa,
  decisión 11); el alta interna del socio con experiments/hu-w09/alta.js en una tarea k6; y su API
  key, del plan estandar al experimento (decisión 2; si falla, el tramo gw irá en estandar).
- `hu_w09_lanzar`: ninguna tarea de HU-W09 viva; redescubre el socio y lanza
  experiments/hu-w09/carga.js en una tarea k6 (`startedBy = hu-w09`). Imprime el ARN, t0 y la hora
  mínima para recoger.
- `hu_w09_recoger`: el log de k6, CloudWatch por etapa (sin su primer minuto), la reacción del
  autoescalado y los digests de las imágenes. Lo deja como JSON en el log (`HU-W09-RECOGIDA `) y
  como tablas en $GITHUB_STEP_SUMMARY, con una evaluación preliminar (manifiesto, C2 a C4) que no
  hace fallar la prueba: solo la hacen fallar los datos que faltan.
- `hu_w09_detener`: detiene toda tarea con `startedBy` que empiece por `hu-w09`.

Guardas, antes de tocar AWS:
1. `-k` con el nombre de la prueba (si no, se omite siempre).
2. `ENV=exp` (si no, se omite siempre). Las salidas de infra/platform son de exp, con el clúster y
   el prefijo `solventa-exp`; si no, o si falta alguna, se omite (o falla con `E2E_REQUERIDO=1`).
3. boto3 con credenciales (`sts:GetCallerIdentity`), con la misma regla.
4. Nunca se imprime el secreto del cliente, la API key, el clientId ni el id de la API key. En
   GitHub Actions se enmascaran con `::add-mask::` antes de usarlos, y todo lo que se imprime, se
   escribe en el resumen o va en un fallo pasa por `Secretos.redactar`. `describe_tasks` devuelve
   las variables de `containerOverrides`: nunca se imprime una tarea entera.

Los scripts de k6 viajan en base64 en la definición de tarea (HU_W09_SCRIPT, infra/apps/carga.tf:
familias <prefijo>-carga y <prefijo>-carga-alta), con el command fijo: sin S3 ni URL prefirmada,
cuya firma y token de sesión k6 imprimía al arrancar. `run_task` solo pasa variables de entorno.

Las pruebas no comparten estado: cada una redescubre en AWS lo que necesita por convención de
nombres. Con más de un candidato, fallan con la lista en vez de elegir (salvo el socio, del que
`hu_w09_lanzar` toma el más reciente, como pide el plan, y nombra los demás).

Las funciones puras tienen sus unitarios al final y no tocan AWS. `politica_de` y `linea_de_tiempo`
son copias de tests/e2e/test_hu_w08_escalado.py (con --import-mode=importlib, un archivo de pruebas
no se importa desde otro); aquí `linea_de_tiempo` compara (deseadas, corriendo) por minuto.
"""

import base64
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import pytest

try:
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - boto3 es dependencia del workspace
    BotoCoreError = ClientError = Exception  # type: ignore[misc,assignment]

HISTORIA = "HU-W09"
AMBIENTE = "exp"
# Prefijo <nombre>-<ambiente> (infra/platform/main.tf:34): clúster, ALB, familia y SG de k6.
CLUSTER = f"solventa-{AMBIENTE}"
SERVICIOS = ("api-socios", "cotizacion", "catalogo", "clientes")  # cadena de R1 por socios
DIMENSION = "ecs:service:DesiredCount"
NAMESPACE = "Solventa"  # var.metrics_namespace (infra/apps/variables.tf:144-147); EXP no lo cambia
CONTENEDOR_K6 = "k6"  # infra/apps/carga.tf: contenedor y awslogs-stream-prefix
INICIADA_POR_ALTA = "hu-w09-alta"
INICIADA_POR_CARGA = "hu-w09"
PREFIJO_TAREAS = "hu-w09"
MOTIVO_DETENER = "HU-W09: detenida por hu_w09_detener"
DIR_SCRIPTS = Path(__file__).parents[2] / "experiments" / "hu-w09"
# Sufijo de la familia de cada definición de tarea k6 (<prefijo>-<sufijo>, infra/apps/carga.tf).
FAMILIAS = {"alta": "carga-alta", "carga": "carga"}
VARIABLE_SCRIPT = "HU_W09_SCRIPT"  # el script en base64, fijo en la definición
# RunTask: «A total of 8192 characters are allowed for overrides» (incluye el JSON).
LIMITE_SOBRESCRITURAS = 8192
# Cuotas de vCPU de Fargate (ServiceCode fargate; Amazon ECS endpoints and quotas, AWS General
# Reference, «AWS Fargate quotas»). EXP no usa Spot (use_fargate_spot = false).
CUOTAS_VCPU = {
    "L-3032A538": "Fargate On-Demand vCPU resource count",
    "L-36FBB829": "Fargate Spot vCPU resource count",
}
# Prefijos de las líneas de experiments/hu-w09/{alta,carga}.js, y el de la recogida.
PREFIJO_ALTA = "HU-W09-ALTA"
PREFIJO_INICIO = "HU-W09-INICIO"
PREFIJO_ETAPA = "HU-W09-ETAPA"
PREFIJO_TOKENS = "HU-W09-TOKENS"
PREFIJO_RECOGIDA = "HU-W09-RECOGIDA"
CAMPOS_ETAPA = (
    "etapa",
    "tramo",
    "inicioS",
    "duracionS",
    "pedidaPorMinuto",
    "solicitudes",
    "logradaPorMinuto",
    "tasaError",
    "p95Ms",
    "p99Ms",
    "dropped",
)
MARCA_SOCIO = (
    "HU-W09"  # alta.js lo pone en el nombre; api-socios lo copia a la descripción de la key
)
DESCARTE_S = 60  # primer minuto de cada etapa: transición (plan §c)
# Manifiesto §1 (propuesta de la 9.3, a confirmar antes de la 9.M).
META_TASA = 0.95
META_ERRORES = 0.01
P99_CUELLO_MS = 1600.0  # latency_budget_ms de socio.cotizar
# Plan estandar de EXP: 50/s = 3.000/min (envs/exp/platform.tfvars:41); se usa el 80 %.
TASA_ESTANDAR_POR_MINUTO = 2400
VENTANA_TAREAS_S = 300  # tareas nuevas atribuibles a una actividad de escalado
LIMITE_RESUMEN = 1_000_000  # $GITHUB_STEP_SUMMARY admite 1 MiB por paso
NOTA_RECORTE = "(recortado: el JSON completo está en el log del job)"
VARIABLES_SECRETAS = ("SOCIO_CLIENT_ID", "SOCIO_KEY_ID", "APIGW_CLIENT_SECRET", "APIGW_API_KEY")
MINUTOS_PARA_RECOGER = 40  # 30 de curva, 5 del tramo gw, pausas y cierre (plan §f, paso 5)
# El job de e2e.yml tiene timeout-minutes: 20, con unos 4 min de preparación.
ESPERA_ALTA_S = int(os.getenv("E2E_W09_ESPERA_ALTA_S", "420"))
ESPERA_ARRANQUE_S = int(os.getenv("E2E_W09_ESPERA_ARRANQUE_S", "300"))
ESPERA_INICIO_S = int(os.getenv("E2E_W09_ESPERA_INICIO_S", "180"))
ESPERA_LOG_S = int(os.getenv("E2E_W09_ESPERA_LOG_S", "60"))
MUESTREO_S = 5.0
MAX_PAGINAS = 50
MOTIVOS = {
    "hu_w09_preparar": "da de alta un socio con una tarea k6 y mueve su API key de plan en EXP",
    "hu_w09_lanzar": "lanza la tarea k6 de la carga en EXP (unos 37 min)",
    "hu_w09_recoger": "recoge los resultados de la carga de EXP",
    "hu_w09_detener": "detiene las tareas k6 de HU-W09 en EXP",
}
# App Auto Scaling nombra las alarmas `TargetTracking-<ResourceId>-AlarmHigh-<uuid>` y `-AlarmLow-`.
_ALARMA = re.compile(r"-Alarm(High|Low)-[0-9A-Fa-f-]+$")
_DESEADAS = re.compile(r"desired count to (\d+)", re.IGNORECASE)
_ESTADO_ALARMA = re.compile(r"to (OK|ALARM|INSUFFICIENT_DATA)\b")
_ID_SOCIO = re.compile(r"SOC-\d{6}")  # api_socios/domain/socios.py:40
_CLAVE_SOCIO = re.compile(r"(?P<prefijo>.+)-socio-(?P<socio>SOC-\d{6})")  # apigateway.py:32
_FIRMA = re.compile(r"(X-Amz-(?:Credential|Security-Token|Signature))=[^&\s\"'<>]+")
_DECODIFICADOR = json.JSONDecoder()


# --- Datos ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Politica:
    nombre: str
    objetivo: float
    enfriamiento_salida: int | None
    enfriamiento_entrada: int | None
    alarma_alta: str
    alarma_baja: str


@dataclass(frozen=True)
class Muestra:
    t: float  # minutos desde t0
    deseadas: int
    corriendo: int | None  # SampleCount de CPUUtilization en ese minuto


@dataclass(frozen=True)
class ResultadoK6:
    inicio: dict[str, Any] | None
    etapas: list[dict[str, Any]]
    tokens: dict[str, Any] | None


@dataclass(frozen=True)
class Ventana:
    etapa: str
    inicio: datetime
    fin: datetime
    desde: datetime | None  # medición: sin el primer minuto, en minutos completos
    hasta: datetime | None


@dataclass(frozen=True)
class Actividad:
    inicio: datetime
    fin: datetime | None
    estado: str
    deseadas: int | None
    causa: str


@dataclass(frozen=True)
class TareaNueva:
    tarea: str
    creada: datetime | None
    en_marcha: datetime


@dataclass(frozen=True)
class Reaccion:
    etapa: str
    servicio: str
    alarma: datetime | None = None
    actividad: datetime | None = None
    capacidad: datetime | None = None
    deteccion_s: float | None = None
    decision_s: float | None = None
    capacidad_s: float | None = None
    total_s: float | None = None
    nota: str = ""


@dataclass(frozen=True)
class Consulta:
    id: str
    clave: str
    espacio: str
    metrica: str
    dimensiones: tuple[tuple[str, str], ...]
    estadistica: str
    periodo: int
    agregado: str = ""  # "" (según la estadística), "min_max" o "serie"
    factor: float = 1.0


@dataclass(frozen=True)
class Medicion:
    cluster: str
    alb: str
    grupo: str
    base_de_datos: str
    cache: tuple[str, ...]


@dataclass(frozen=True)
class TramoGw:
    url: str | None
    token_url: str | None
    scope: str | None
    mtls: bool
    client_secret: str
    api_key: str


@dataclass(frozen=True)
class Contexto:
    region: str
    cluster: str
    prefijo: str
    vpc: str
    subredes: tuple[str, ...]
    puerto_alb: int
    planes: dict[str, str]
    eventos: str
    tema_alertas: str
    pool_socios: str
    medicion: Medicion
    api_socios: str | None
    token_url: str | None
    scopes: tuple[str, ...]
    mtls: bool


# --- Funciones puras (con unitarios al final) ----------------------------------------------


class Secretos:
    """Lo que nunca se imprime. `agregar` lo enmascara en GitHub Actions (`::add-mask::`, una
    orden por línea del valor) antes de usarlo; todo lo que sale pasa por `redactar`, que además
    oculta la credencial, el token y la firma de cualquier URL prefirmada (defensa: las pruebas ya
    no generan ninguna)."""

    def __init__(self, emitir: Callable[[str], None], en_actions: bool) -> None:
        self._emitir = emitir
        self._en_actions = en_actions
        self._valores: list[str] = []

    def agregar(self, *valores: str | None) -> None:
        for valor in valores:
            if not valor or valor in self._valores:
                continue
            self._valores.append(valor)
            if self._en_actions:
                for linea in valor.splitlines():
                    if linea.strip():
                        self._emitir(f"::add-mask::{linea}")

    def redactar(self, texto: str) -> str:
        for valor in sorted(self._valores, key=len, reverse=True):
            texto = texto.replace(valor, "***")
        return _FIRMA.sub(r"\1=***", texto)

    def imprimir(self, texto: str) -> None:
        print(self.redactar(texto))

    def resumen(self, texto: str) -> None:
        archivo = os.getenv("GITHUB_STEP_SUMMARY")
        if not archivo:
            return
        with open(archivo, "a", encoding="utf-8") as salida:
            salida.write(f"{limitar(self.redactar(texto))}\n\n")

    def fallar(self, motivo: str) -> NoReturn:
        pytest.fail(self.redactar(motivo))


def seleccionada(keyword: str | None, nombre: str) -> bool:
    """Si la sesión eligió esta prueba a propósito: `-k` con su propio nombre."""
    return nombre in (keyword or "")


def politica_de(respuesta: dict[str, Any], nombre: str) -> Politica:
    """Copia de test_hu_w08_escalado.py: la política `nombre` con su alarma alta y su baja, tal
    como las devuelve `DescribeScalingPolicies` en `Alarms` (el nombre nunca se construye).

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


def linea_de_tiempo(muestras: Iterable[Muestra]) -> list[Muestra]:
    """Copia de test_hu_w08_escalado.py: las muestras en las que cambia (deseadas, corriendo),
    más la última."""
    lista = list(muestras)
    cambios = [
        m
        for i, m in enumerate(lista)
        if i == 0 or (m.deseadas, m.corriendo) != (lista[i - 1].deseadas, lista[i - 1].corriendo)
    ]
    if lista and cambios[-1] is not lista[-1]:
        cambios.append(lista[-1])
    return cambios


def lineas_con_prefijo(lineas: Iterable[str], prefijo: str) -> list[dict[str, Any]]:
    """El objeto JSON de cada línea con `prefijo` seguido de un espacio (admite texto antes, como
    una marca de tiempo). `ValueError` si no trae un objeto JSON; el mensaje no copia la línea."""
    marca = f"{prefijo} "
    objetos = []
    for linea in lineas:
        posicion = linea.find(marca)
        if posicion < 0:
            continue
        texto = linea[posicion + len(marca) :].strip()
        try:
            valor, _ = _DECODIFICADOR.raw_decode(texto)
        except json.JSONDecodeError as error:
            raise ValueError(f"una línea {prefijo} no trae JSON válido ({error.msg})") from None
        if isinstance(valor, dict):
            objetos.append(valor)
            continue
        raise ValueError(f"una línea {prefijo} no trae un objeto JSON")
    return objetos


def leer_resultado_k6(lineas: Iterable[str]) -> ResultadoK6:
    """Las líneas HU-W09-INICIO, HU-W09-ETAPA y HU-W09-TOKENS del log de carga.js."""
    lista = list(lineas)
    inicios = lineas_con_prefijo(lista, PREFIJO_INICIO)
    if len(inicios) > 1:
        raise ValueError(f"{len(inicios)} líneas {PREFIJO_INICIO} en el log (¿dos corridas?)")
    inicio = inicios[0] if inicios else None
    if inicio is not None and not inicio.get("t0"):
        raise ValueError(f"la línea {PREFIJO_INICIO} no trae t0")
    etapas = lineas_con_prefijo(lista, PREFIJO_ETAPA)
    nombres = [str(e.get("etapa")) for e in etapas]
    repetidas = sorted({n for n in nombres if nombres.count(n) > 1})
    if repetidas:
        raise ValueError(f"etapas repetidas en el log: {', '.join(repetidas)}")
    for etapa in etapas:
        faltan = [campo for campo in CAMPOS_ETAPA if campo not in etapa]
        if faltan:
            raise ValueError(f"la etapa {etapa.get('etapa')} no trae {', '.join(faltan)}")
    tokens = lineas_con_prefijo(lista, PREFIJO_TOKENS)
    return ResultadoK6(inicio, etapas, tokens[-1] if tokens else None)


def leer_alta(lineas: Iterable[str]) -> tuple[str, str]:
    """(id, clientId) de la línea HU-W09-ALTA de alta.js. Los errores no muestran el clientId."""
    altas = lineas_con_prefijo(list(lineas), PREFIJO_ALTA)
    if not altas:
        raise LookupError(f"el log de k6 no trae la línea {PREFIJO_ALTA}")
    if len(altas) > 1:
        raise ValueError(f"{len(altas)} líneas {PREFIJO_ALTA} en el log; se esperaba una")
    socio, client_id = altas[0].get("id"), altas[0].get("clientId")
    if not isinstance(socio, str) or not _ID_SOCIO.fullmatch(socio):
        raise ValueError(f"id de socio inesperado en {PREFIJO_ALTA}: {socio!r}")
    if not isinstance(client_id, str) or not client_id:
        raise ValueError(f"{PREFIJO_ALTA} no trae clientId")
    return socio, client_id


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


def _minuto_abajo(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def _minuto_arriba(t: datetime) -> datetime:
    abajo = _minuto_abajo(t)
    return abajo if abajo == t else abajo + timedelta(minutes=1)


def ventanas(
    t0: datetime, etapas: Iterable[dict[str, Any]], descarte_s: int = DESCARTE_S
) -> list[Ventana]:
    """Inicio y fin de cada etapa desde t0, y su ventana de medición: sin el primer minuto y solo
    con minutos completos (los puntos de 1 min de CloudWatch empiezan en el minuto). Sin ventana
    (None) si no queda al menos un minuto."""
    resultado = []
    for etapa in etapas:
        inicio = t0 + timedelta(seconds=float(etapa["inicioS"]))
        fin = inicio + timedelta(seconds=float(etapa["duracionS"]))
        desde: datetime | None = _minuto_arriba(inicio + timedelta(seconds=descarte_s))
        hasta: datetime | None = _minuto_abajo(fin)
        if hasta - desde < timedelta(minutes=1):  # type: ignore[operator]
            desde = hasta = None
        resultado.append(Ventana(str(etapa["etapa"]), inicio, fin, desde, hasta))
    return sorted(resultado, key=lambda v: v.inicio)


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


def actividades_de(items: Iterable[dict[str, Any]]) -> list[Actividad]:
    """Las actividades de DescribeScalingActivities, en orden, con el desiredCount que fijaron."""
    actividades = []
    for item in items:
        deseadas = _DESEADAS.search(str(item.get("Description", "")))
        actividades.append(
            Actividad(
                instante(item["StartTime"]),
                instante(item["EndTime"]) if item.get("EndTime") else None,
                str(item.get("StatusCode", "")),
                int(deseadas.group(1)) if deseadas else None,
                str(item.get("Cause", "")),
            )
        )
    return sorted(actividades, key=lambda a: a.inicio)


def tareas_en_marcha(eventos: Iterable[dict[str, Any]]) -> dict[str, list[TareaNueva]]:
    """Por servicio, la primera hora en RUNNING de cada tarea, de los eventos `ECS Task State
    Change` del events_log_group (infra/platform/observability.tf:39-71)."""
    por_tarea: dict[tuple[str, str], TareaNueva] = {}
    for evento in eventos:
        if evento.get("detail-type") != "ECS Task State Change":
            continue
        detalle = evento.get("detail") or {}
        grupo = str(detalle.get("group", ""))
        if detalle.get("lastStatus") != "RUNNING" or not grupo.startswith("service:"):
            continue
        tarea = str(detalle.get("taskArn", "")).rsplit("/", 1)[-1]
        momento = detalle.get("startedAt") or evento.get("time")
        if not tarea or not momento:
            continue
        creada = instante(detalle["createdAt"]) if detalle.get("createdAt") else None
        nueva = TareaNueva(tarea, creada, instante(momento))
        clave = (grupo.removeprefix("service:"), tarea)
        if clave not in por_tarea or nueva.en_marcha < por_tarea[clave].en_marcha:
            por_tarea[clave] = nueva
    resultado: dict[str, list[TareaNueva]] = {}
    for (servicio, _), tarea_nueva in por_tarea.items():
        resultado.setdefault(servicio, []).append(tarea_nueva)
    return {s: sorted(ts, key=lambda t: t.en_marcha) for s, ts in resultado.items()}


def deseadas_en(t: datetime, actividades: Iterable[Actividad], inicial: int) -> int:
    """El desiredCount vigente justo antes de `t`: el de la última actividad exitosa anterior."""
    valor = inicial
    for actividad in sorted(actividades, key=lambda a: a.inicio):
        if (
            actividad.inicio < t
            and actividad.estado == "Successful"
            and actividad.deseadas is not None
        ):
            valor = actividad.deseadas
    return valor


def _segundos(desde: datetime, hasta: datetime) -> float:
    return round((hasta - desde).total_seconds(), 1)


def reaccion(
    ventana: Ventana,
    servicio: str,
    cambios: list[tuple[datetime, str]],
    actividades: list[Actividad],
    tareas: list[TareaNueva],
    inicial: int,
    maximo: int,
) -> Reaccion:
    """Reacción del autoescalado de `servicio` en una etapa (exploración E.14): detección = ALARM
    de la alarma alta − inicio de la etapa (0 si ya estaba en ALARM); decisión = inicio de la
    primera actividad hacia fuera − ALARM; capacidad = RUNNING de la última tarea nueva de esa
    actividad − su inicio; total = ese RUNNING − inicio de la etapa. «Al tope» si el desiredCount
    ya estaba en el máximo al empezar."""
    etapa = ventana.etapa
    al_empezar = deseadas_en(ventana.inicio, actividades, inicial)
    if al_empezar >= maximo:
        return Reaccion(
            etapa, servicio, nota=f"al tope: {al_empezar} de {maximo} tareas al empezar la etapa"
        )
    notas = []
    previos = [estado for t, estado in cambios if t < ventana.inicio]
    if previos and previos[-1] == "ALARM":
        alarma: datetime | None = ventana.inicio
        notas.append("ya en ALARM al empezar la etapa")
    else:
        alarma = next(
            (t for t, estado in cambios if estado == "ALARM" and ventana.inicio <= t < ventana.fin),
            None,
        )
    if alarma is None:
        return Reaccion(etapa, servicio, nota="sin ALARM en la etapa: no hizo falta escalar")
    deteccion = _segundos(ventana.inicio, alarma)
    hacia_fuera = [
        a
        for a in actividades
        if alarma <= a.inicio < ventana.fin
        and a.estado != "Failed"
        and a.deseadas is not None
        and a.deseadas > deseadas_en(a.inicio, actividades, inicial)
    ]
    if not hacia_fuera:
        notas.append("ALARM sin actividad de escalado hacia fuera en la etapa")
        return Reaccion(etapa, servicio, alarma, deteccion_s=deteccion, nota="; ".join(notas))
    actividad = hacia_fuera[0]
    decision = _segundos(alarma, actividad.inicio)
    if actividad.deseadas is not None and actividad.deseadas >= maximo:
        notas.append(f"llegó al máximo ({maximo})")
    siguiente = next((a.inicio for a in actividades if a.inicio > actividad.inicio), None)
    limite = siguiente or actividad.inicio + timedelta(seconds=VENTANA_TAREAS_S)
    nuevas = [t for t in tareas if actividad.inicio <= (t.creada or t.en_marcha) < limite]
    if not nuevas:
        notas.append("sin tarea nueva en RUNNING tras la actividad")
        return Reaccion(
            etapa,
            servicio,
            alarma,
            actividad.inicio,
            None,
            deteccion,
            decision,
            nota="; ".join(notas),
        )
    capacidad = max(t.en_marcha for t in nuevas)
    return Reaccion(
        etapa,
        servicio,
        alarma,
        actividad.inicio,
        capacidad,
        deteccion,
        decision,
        _segundos(actividad.inicio, capacidad),
        _segundos(ventana.inicio, capacidad),
        "; ".join(notas),
    )


def muestras_de(
    serie: Iterable[tuple[datetime, float]],
    actividades: list[Actividad],
    inicial: int,
    t0: datetime,
) -> list[Muestra]:
    """Por minuto: el desiredCount al cerrar el minuto y las tareas que reportaron CPU."""
    return [
        Muestra(
            round((t - t0).total_seconds() / 60, 1),
            deseadas_en(t + timedelta(minutes=1), actividades, inicial),
            None if valor is None else round(valor),
        )
        for t, valor in sorted(serie)
    ]


def consultas_de_etapa(medicion: Medicion, periodo_s: int) -> list[Consulta]:
    """Las consultas de GetMetricData de una etapa (plan §c), con un periodo igual a la ventana
    (un punto por consulta) salvo las tareas, por minuto (mínimo y máximo)."""
    consultas: list[Consulta] = []

    def agregar(
        clave: str,
        espacio: str,
        metrica: str,
        dimensiones: Iterable[tuple[str, str]],
        estadistica: str,
        periodo: int = periodo_s,
        agregado: str = "",
        factor: float = 1.0,
    ) -> None:
        consultas.append(
            Consulta(
                f"q{len(consultas)}",
                clave,
                espacio,
                metrica,
                tuple(dimensiones),
                estadistica,
                periodo,
                agregado,
                factor,
            )
        )

    for etiqueta, servicio, operacion in (
        ("cotizacion", "cotizacion", "cotizacion"),
        ("socio", "api-socios", "socio.cotizar"),
    ):
        dimensiones = (("Environment", AMBIENTE), ("Service", servicio), ("Operation", operacion))
        agregar(f"{etiqueta}.p95", NAMESPACE, "OperationDuration", dimensiones, "p95")
        agregar(f"{etiqueta}.p99", NAMESPACE, "OperationDuration", dimensiones, "p99")
        agregar(f"{etiqueta}.n", NAMESPACE, "OperationDuration", dimensiones, "SampleCount")
        agregar(f"{etiqueta}.errores", NAMESPACE, "OperationErrors", dimensiones, "Sum")
    elb = "AWS/ApplicationELB"
    alb = (("LoadBalancer", medicion.alb), ("TargetGroup", medicion.grupo))
    agregar("alb.solicitudes", elb, "RequestCount", alb, "Sum")
    agregar("alb.p95", elb, "TargetResponseTime", alb, "p95", factor=1000)
    agregar("alb.p99", elb, "TargetResponseTime", alb, "p99", factor=1000)
    agregar("alb.5xx_destino", elb, "HTTPCode_Target_5XX_Count", alb, "Sum")
    agregar("alb.5xx_elb", elb, "HTTPCode_ELB_5XX_Count", (("LoadBalancer", medicion.alb),), "Sum")
    for servicio in SERVICIOS:
        dimensiones = (("ClusterName", medicion.cluster), ("ServiceName", servicio))
        agregar(
            f"tareas.{servicio}",
            "AWS/ECS",
            "CPUUtilization",
            dimensiones,
            "SampleCount",
            60,
            "min_max",
        )
        for clave, metrica in (("cpu", "CPUUtilization"), ("mem", "MemoryUtilization")):
            agregar(f"{clave}.{servicio}.prom", "AWS/ECS", metrica, dimensiones, "Average")
            agregar(f"{clave}.{servicio}.max", "AWS/ECS", metrica, dimensiones, "Maximum")
    rds = (("DBInstanceIdentifier", medicion.base_de_datos),)
    for clave, metrica in (("cpu", "CPUUtilization"), ("conexiones", "DatabaseConnections")):
        agregar(f"rds.{clave}.prom", "AWS/RDS", metrica, rds, "Average")
        agregar(f"rds.{clave}.max", "AWS/RDS", metrica, rds, "Maximum")
    for cache in medicion.cache:
        dimensiones = (("CacheClusterId", cache),)
        agregar(
            f"valkey.{cache}.cpu.prom",
            "AWS/ElastiCache",
            "EngineCPUUtilization",
            dimensiones,
            "Average",
        )
        agregar(
            f"valkey.{cache}.cpu.max",
            "AWS/ElastiCache",
            "EngineCPUUtilization",
            dimensiones,
            "Maximum",
        )
    return consultas


def consultas_de_tareas(medicion: Medicion) -> list[Consulta]:
    """Tareas por minuto de cada servicio (SampleCount de CPUUtilization; sin Container Insights,
    decisión 13), para la línea de tiempo de toda la corrida."""
    return [
        Consulta(
            f"t{i}",
            servicio,
            "AWS/ECS",
            "CPUUtilization",
            (("ClusterName", medicion.cluster), ("ServiceName", servicio)),
            "SampleCount",
            60,
            "serie",
        )
        for i, servicio in enumerate(SERVICIOS)
    ]


def a_consultas_aws(consultas: Iterable[Consulta]) -> list[dict[str, Any]]:
    return [
        {
            "Id": c.id,
            "MetricStat": {
                "Metric": {
                    "Namespace": c.espacio,
                    "MetricName": c.metrica,
                    "Dimensions": [{"Name": n, "Value": v} for n, v in c.dimensiones],
                },
                "Period": c.periodo,
                "Stat": c.estadistica,
            },
            "ReturnData": True,
        }
        for c in consultas
    ]


def _redondo(valor: float) -> float:
    return round(valor, 1)


def resumir(consultas: Iterable[Consulta], resultados: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """El valor de cada consulta (por su clave) a partir de MetricDataResults, que pueden venir
    en varias páginas. Sin agregado explícito: suma para Sum y SampleCount, media para Average y
    máximo para el resto (si CloudWatch partiera la ventana, el percentil más alto)."""
    puntos_por_id: dict[str, list[tuple[datetime, float]]] = {}
    for resultado in resultados:
        puntos_por_id.setdefault(resultado["Id"], []).extend(
            (instante(t), float(v))
            for t, v in zip(
                resultado.get("Timestamps", []), resultado.get("Values", []), strict=False
            )
        )
    salida: dict[str, Any] = {}
    for consulta in consultas:
        puntos = sorted((t, v * consulta.factor) for t, v in puntos_por_id.get(consulta.id, []))
        numeros = [v for _, v in puntos]
        agregado = consulta.agregado or {
            "Sum": "suma",
            "SampleCount": "suma",
            "Average": "media",
        }.get(consulta.estadistica, "max")
        if agregado == "serie":
            salida[consulta.clave] = [[t.isoformat(), _redondo(v)] for t, v in puntos]
        elif not numeros:
            salida[consulta.clave] = None
        elif agregado == "min_max":
            salida[consulta.clave] = {"min": _redondo(min(numeros)), "max": _redondo(max(numeros))}
        elif agregado == "suma":
            salida[consulta.clave] = _redondo(sum(numeros))
        elif agregado == "media":
            salida[consulta.clave] = _redondo(sum(numeros) / len(numeros))
        else:
            salida[consulta.clave] = _redondo(max(numeros))
    return salida


def unico(candidatos: Iterable[Any], que: str, describir: Callable[[Any], str] = str) -> Any:
    """El único candidato; si no hay ninguno o hay varios, falla con la lista en vez de elegir."""
    lista = list(candidatos)
    if not lista:
        raise LookupError(f"ningún {que}")
    if len(lista) > 1:
        raise ValueError(
            f"{len(lista)} {que}: {', '.join(describir(c) for c in lista)}; se esperaba uno"
        )
    return lista[0]


def socio_mas_reciente(
    claves: Iterable[dict[str, Any]], prefijo: str
) -> tuple[str, str, list[str]]:
    """(socio, id de su API key, socios anteriores) del socio de HU-W09 más reciente: la API key
    `<prefijo>-socio-SOC-nnnnnn` habilitada, con la etiqueta `Socio` igual y la marca de alta.js en
    la descripción (apigateway.py de api-socios). Dos con la misma fecha: falla sin elegir."""
    candidatas = []
    for clave in claves:
        encontrada = _CLAVE_SOCIO.fullmatch(str(clave.get("name", "")))
        if not encontrada or encontrada.group("prefijo") != prefijo:
            continue
        socio = encontrada.group("socio")
        if (
            (clave.get("tags") or {}).get("Socio") != socio
            or MARCA_SOCIO not in str(clave.get("description", ""))
            or not clave.get("enabled")
        ):
            continue
        candidatas.append((instante(clave["createdDate"]), socio, str(clave["id"])))
    if not candidatas:
        raise LookupError(
            f"ninguna API key {prefijo}-socio-SOC-… habilitada, con la etiqueta Socio y "
            "creada por alta.js: ¿corrió hu_w09_preparar?"
        )
    candidatas.sort()
    if len(candidatas) > 1 and candidatas[-1][0] == candidatas[-2][0]:
        empatados = sorted(s for t, s, _ in candidatas if t == candidatas[-1][0])
        raise ValueError(
            f"varios socios de HU-W09 creados a la vez: {', '.join(empatados)}; no se elige"
        )
    _, socio, key_id = candidatas[-1]
    return socio, key_id, [s for _, s, _ in candidatas[:-1]]


def scope_cotizar(scopes: Iterable[str]) -> str | None:
    """El scope de cotizar de cognito.partners.scopes (`solventa/cotizar`), si hay uno solo."""
    cotizar = [s for s in scopes if s.endswith("/cotizar")]
    return cotizar[0] if len(cotizar) == 1 else None


def variables_de_carga(
    alb_url: str, client_id: str, key_id: str, plan: str, gw: TramoGw
) -> tuple[dict[str, str], str]:
    """Las variables de carga.js y por qué el tramo gw va, va en estandar o no va."""
    variables = {"ALB_URL": alb_url, "SOCIO_CLIENT_ID": client_id, "SOCIO_KEY_ID": key_id}
    if gw.mtls:
        return variables, (
            "sin tramo gw: el API de socios exige mTLS (salida api_urls.socios_mtls) "
            "y la tarea k6 no tiene certificado de cliente"
        )
    faltan = [
        nombre
        for nombre, valor in (
            ("api_urls.socios", gw.url),
            ("cognito.partners.token_url", gw.token_url),
            ("el scope cotizar de cognito.partners.scopes", gw.scope),
            ("el secreto del cliente de Cognito", gw.client_secret),
            ("el valor de la API key", gw.api_key),
        )
        if not valor
    ]
    if faltan:
        return variables, f"sin tramo gw: falta {', '.join(faltan)}"
    if plan not in ("experimento", "estandar"):
        return (
            variables,
            f"sin tramo gw: la API key no está en ningún plan de uso utilizable ({plan})",
        )
    variables.update(
        {
            "APIGW_URL": str(gw.url),
            "APIGW_TOKEN_URL": str(gw.token_url),
            "APIGW_CLIENT_SECRET": gw.client_secret,
            "APIGW_API_KEY": gw.api_key,
            "APIGW_SCOPE": str(gw.scope),
        }
    )
    if plan == "estandar":
        variables["APIGW_TASA"] = str(TASA_ESTANDAR_POR_MINUTO)
        return variables, (
            "tramo gw en estandar (decisión 2, opción c): APIGW_TASA = 2.400/min, el 80 % "
            "del límite del plan estandar de EXP (50/s = 3.000/min, "
            "envs/exp/platform.tfvars:41)"
        )
    return variables, (
        "tramo gw en experimento (decisión 2): APIGW_TASA por omisión de carga.js, "
        "6.000/min (el 60 % del límite del WAF por IP)"
    )


def valores_secretos(variables: dict[str, str]) -> list[str]:
    return [variables[nombre] for nombre in VARIABLES_SECRETAS if variables.get(nombre)]


def sobrescrituras(variables: dict[str, str]) -> dict[str, Any]:
    """Las overrides de run_task: solo variables de entorno del contenedor k6 (el command y el
    script van fijos en la definición). `ValueError` si miden 8.192 caracteres o más (RunTask,
    `overrides`; se mide con json.dumps, con sus espacios); el mensaje no muestra valores."""
    overrides = {
        "containerOverrides": [
            {
                "name": CONTENEDOR_K6,
                "environment": [{"name": k, "value": v} for k, v in sorted(variables.items())],
            }
        ]
    }
    medida = len(json.dumps(overrides))
    if medida >= LIMITE_SOBRESCRITURAS:
        raise ValueError(
            f"las containerOverrides miden {medida} caracteres y RunTask admite menos de "
            f"{LIMITE_SOBRESCRITURAS} (variables: {', '.join(sorted(variables))})"
        )
    return overrides


def huella_del_script(
    definicion: dict[str, Any], tarea: dict[str, Any] | None, local: bytes | None
) -> dict[str, Any]:
    """Para el manifiesto: la revisión que corrió, el sha256 del script que lleva (HU_W09_SCRIPT
    decodificado), si coincide con el de experiments/hu-w09/ del commit y la imagen de k6 con su
    digest (el de la tarea o, si no, el de la referencia de la definición)."""
    k6 = next(
        (c for c in definicion.get("containerDefinitions", []) if c.get("name") == CONTENEDOR_K6),
        {},
    )
    imagenes = imagenes_de({"containerDefinitions": [k6]} if k6 else {}, tarea)
    imagen = imagenes[0] if imagenes else {}
    huella: dict[str, Any] = {
        "revision": str(definicion.get("taskDefinitionArn", "")).rsplit("/", 1)[-1] or None,
        "sha256": None,
        "sha256Commit": None if local is None else hashlib.sha256(local).hexdigest(),
        "coincide": None,
        "imagen": imagen.get("imagen"),
        "digest": imagen.get("digest"),
        "nota": "",
    }
    valor = next(
        (v.get("value") for v in k6.get("environment") or [] if v.get("name") == VARIABLE_SCRIPT),
        None,
    )
    if not valor:
        return {**huella, "coincide": False, "nota": f"la definición no trae {VARIABLE_SCRIPT}"}
    try:
        script = base64.b64decode(valor, validate=True)
    except ValueError:
        return {**huella, "coincide": False, "nota": f"{VARIABLE_SCRIPT} no es base64 válido"}
    huella["sha256"] = hashlib.sha256(script).hexdigest()
    if local is None:
        return {**huella, "nota": "sin el script del commit para comparar"}
    huella["coincide"] = huella["sha256"] == huella["sha256Commit"]
    if not huella["coincide"]:
        huella["nota"] = (
            "el script de la revisión es distinto del de experiments/hu-w09/ del commit"
        )
    return huella


def _unidades(valor: Any) -> int | None:
    try:
        return int(str(valor))
    except (TypeError, ValueError):
        return None


def necesidad_vcpu(servicios: Iterable[dict[str, Any]], k6_cpu: Any) -> dict[str, Any]:
    """vCPU de Fargate que pide la cadena ahora (desiredCount) y al máximo de su scalable target,
    más la tarea k6 (unidades de CPU de cada definición / 1024). `faltan` nombra lo que no se
    pudo leer y no se suma."""
    actual = maxima = 0
    faltan = []
    for servicio in servicios:
        cpu = _unidades(servicio.get("cpu"))
        deseadas, maximo = servicio.get("deseadas"), servicio.get("maximo")
        if cpu is None or deseadas is None or maximo is None:
            faltan.append(str(servicio.get("servicio")))
            continue
        actual += cpu * int(deseadas)
        maxima += cpu * int(maximo)
    k6 = _unidades(k6_cpu)
    if k6 is None:
        faltan.append("k6")
    else:
        actual += k6
        maxima += k6
    return {
        "actual": round(actual / 1024, 2),
        "maxima": round(maxima / 1024, 2),
        "k6": None if k6 is None else round(k6 / 1024, 2),
        "faltan": faltan,
    }


def filas_de_cuotas(
    cuotas: dict[str, dict[str, Any]], necesidad: dict[str, Any]
) -> list[tuple[str, str]]:
    """Las cuotas de vCPU de Fargate leídas (o por qué no) junto a la necesidad estimada, y si la
    On-Demand alcanza (EXP no usa Spot)."""
    filas = []
    for codigo, nombre in CUOTAS_VCPU.items():
        cuota = cuotas.get(codigo) or {}
        valor = cuota.get("valor")
        texto = f"{valor:g} vCPU ({cuota.get('origen')})" if valor is not None else None
        filas.append(
            (
                f"Cuota {cuota.get('nombre', nombre)} ({codigo})",
                texto or cuota.get("origen", "no se leyó"),
            )
        )
    estimada = (
        f"{necesidad['actual']:g} ahora · {necesidad['maxima']:g} con la cadena al máximo "
        f"(k6: {_celda(necesidad.get('k6'))})"
    )
    if necesidad.get("faltan"):
        estimada += f"; faltan: {', '.join(necesidad['faltan'])}"
    filas.append(("vCPU estimadas (cadena y k6)", estimada))
    bajo_demanda = (cuotas.get("L-3032A538") or {}).get("valor")
    if bajo_demanda is None:
        holgura = "sin la cuota On-Demand: no se puede comparar"
    elif necesidad["maxima"] > bajo_demanda:
        holgura = (
            f"no alcanza: la cadena al máximo con k6 pide {necesidad['maxima']:g} vCPU y la cuota "
            f"es {bajo_demanda:g}; el escalado o run_task pueden fallar por cuota"
        )
    else:
        holgura = f"alcanza: {necesidad['maxima']:g} de {bajo_demanda:g} vCPU"
    filas.append(("Holgura On-Demand", holgura))
    return filas


def contexto_de(salidas: dict[str, Any]) -> Contexto:
    """Lo que las pruebas necesitan de las salidas de infra/platform. `ValueError` si no son de
    exp con el clúster y el prefijo solventa-exp, o si falta algo (con la lista)."""
    plataforma = salidas.get("platform") or {}
    if not plataforma:
        raise ValueError(
            "faltan las salidas de infra/platform (terraform output -json del ambiente)"
        )
    cluster = (plataforma.get("cluster") or {}).get("name")
    if (plataforma.get("environment"), cluster, plataforma.get("prefix")) != (
        AMBIENTE,
        CLUSTER,
        CLUSTER,
    ):
        raise ValueError(
            f"las salidas de infra/platform son de {plataforma.get('environment')!r}, con el clúster "
            f"{cluster!r} y el prefijo {plataforma.get('prefix')!r}; se esperaba exp y {CLUSTER}"
        )
    faltan: list[str] = []

    def leer(nombre: str, valor: Any) -> Any:
        if valor is None or valor == "" or (isinstance(valor, list | dict | tuple) and not valor):
            faltan.append(nombre)
        return valor

    catalogo = plataforma.get("catalog") or {}
    puerto = leer(
        "catalog[api-socios].listener_port", (catalogo.get("api-socios") or {}).get("listener_port")
    )
    subredes = leer("private_subnet_ids", plataforma.get("private_subnet_ids"))
    vpc = leer("vpc_id", plataforma.get("vpc_id"))
    planes = leer("usage_plans", plataforma.get("usage_plans"))
    if planes and "estandar" not in planes:
        faltan.append("usage_plans.estandar")
    eventos = leer("events_log_group", plataforma.get("events_log_group"))
    tema = leer(
        "alerts_topic_arn", plataforma.get("alerts_topic_arn") or salidas.get("alerts_topic_arn")
    )
    pool = leer(
        "cognito.partners_pool_id", (plataforma.get("cognito") or {}).get("partners_pool_id")
    )
    alb = leer("alb.arn_suffix", (plataforma.get("alb") or {}).get("arn_suffix"))
    grupo = leer(
        "target_groups[api-socios].arn_suffix",
        ((plataforma.get("target_groups") or {}).get("api-socios") or {}).get("arn_suffix"),
    )
    base = leer("database.identifier", (plataforma.get("database") or {}).get("identifier"))
    if faltan:
        raise ValueError(f"faltan en las salidas de infra/platform: {', '.join(faltan)}")
    socios = (salidas.get("cognito") or {}).get("partners") or {}
    urls = salidas.get("api_urls") or {}
    return Contexto(
        region=plataforma.get("region") or os.getenv("AWS_REGION", "us-east-1"),
        cluster=str(cluster),
        prefijo=str(plataforma["prefix"]),
        vpc=str(vpc),
        subredes=tuple(subredes),
        puerto_alb=int(puerto),
        planes=dict(planes),
        eventos=str(eventos),
        tema_alertas=str(tema),
        pool_socios=str(pool),
        medicion=Medicion(
            str(cluster),
            str(alb),
            str(grupo),
            str(base),
            tuple((plataforma.get("cache") or {}).get("cluster_ids") or ()),
        ),
        api_socios=urls.get("socios") or None,
        token_url=socios.get("token_url")
        or (plataforma.get("cognito") or {}).get("partners_token_url"),
        scopes=tuple(socios.get("scopes") or ()),
        mtls=bool(urls.get("socios_mtls")),
    )


def estado_de_la_cadena(respuesta: dict[str, Any]) -> list[dict[str, Any]]:
    """Por servicio de la cadena (DescribeServices): estable si está ACTIVE, con tareas,
    runningCount == desiredCount y un solo despliegue."""
    por_nombre = {s.get("serviceName"): s for s in respuesta.get("services", [])}
    filas = []
    for nombre in SERVICIOS:
        servicio = por_nombre.get(nombre) or {}
        deseadas, corriendo = servicio.get("desiredCount"), servicio.get("runningCount")
        despliegues = len(servicio.get("deployments") or [])
        filas.append(
            {
                "servicio": nombre,
                "estado": servicio.get("status", "no existe"),
                "deseadas": deseadas,
                "corriendo": corriendo,
                "despliegues": despliegues,
                "estable": (
                    servicio.get("status") == "ACTIVE"
                    and bool(deseadas)
                    and corriendo == deseadas
                    and despliegues == 1
                ),
            }
        )
    return filas


def estado_suscripciones(suscripciones: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Cuántas suscripciones del tema de alertas están confirmadas o pendientes (decisión 11).
    Nunca devuelve el Endpoint (el correo)."""
    lista = list(suscripciones)
    confirmadas = sum(1 for s in lista if str(s.get("SubscriptionArn", "")).startswith("arn:"))
    pendientes = sum(1 for s in lista if s.get("SubscriptionArn") == "PendingConfirmation")
    partes = []
    if confirmadas:
        partes.append(f"{confirmadas} confirmada{'s' if confirmadas != 1 else ''}")
    if pendientes:
        partes.append(f"{pendientes} PendingConfirmation")
    return {
        "confirmadas": confirmadas,
        "pendientes": pendientes,
        "protocolos": sorted({str(s["Protocol"]) for s in lista if s.get("Protocol")}),
        "estado": ", ".join(partes) or "sin suscripciones",
    }


def plan_efectivo(key_id: str, claves_por_plan: dict[str, Iterable[str]]) -> str:
    planes = sorted(nombre for nombre, ids in claves_por_plan.items() if key_id in set(ids))
    if not planes:
        return "ninguno"
    return planes[0] if len(planes) == 1 else f"varios: {', '.join(planes)}"


def tareas_hu_w09(
    tareas: Iterable[dict[str, Any]], exacto: str | None = None
) -> list[dict[str, Any]]:
    """Las tareas con startedBy que empieza por hu-w09 (o igual a `exacto`)."""
    if exacto:
        return [t for t in tareas if t.get("startedBy") == exacto]
    return [t for t in tareas if str(t.get("startedBy") or "").startswith(PREFIJO_TAREAS)]


def ultima(tareas: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    return max(tareas, key=lambda t: instante(t["createdAt"]), default=None)


def salida_de(tarea: dict[str, Any]) -> dict[str, Any]:
    """Cómo terminó la tarea k6, sin las variables de containerOverrides."""
    k6 = next((c for c in tarea.get("containers", []) if c.get("name") == CONTENEDOR_K6), {})
    return {
        "estado": tarea.get("lastStatus"),
        "exitCode": k6.get("exitCode"),
        "stopCode": tarea.get("stopCode"),
        "stoppedReason": tarea.get("stoppedReason"),
        "motivoContenedor": k6.get("reason"),
    }


def _digest_de(imagen: str | None) -> str | None:
    return imagen.split("@", 1)[1] if imagen and "@sha256:" in imagen else None


def imagenes_de(definicion: dict[str, Any], tarea: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Imagen de cada contenedor de la definición y su digest: el de una tarea en marcha o, si
    no, el de la propia referencia (`imagen@sha256:…`)."""
    digests = {c.get("name"): c.get("imageDigest") for c in (tarea or {}).get("containers", [])}
    return [
        {
            "contenedor": c.get("name"),
            "imagen": c.get("image"),
            "digest": digests.get(c.get("name")) or _digest_de(c.get("image")),
        }
        for c in definicion.get("containerDefinitions", [])
    ]


def flujo_k6(arn: str) -> str:
    """El flujo de awslogs de la tarea: <stream-prefix>/<contenedor>/<id> (infra/apps/carga.tf)."""
    return f"{CONTENEDOR_K6}/{CONTENEDOR_K6}/{arn.rsplit('/', 1)[-1]}"


def validez(etapa: dict[str, Any]) -> str:
    """Regla de etapa válida del manifiesto (decisión 15)."""
    if not etapa.get("dropped"):
        return "válida"
    p99 = etapa.get("p99Ms")
    if p99 is not None and p99 >= P99_CUELLO_MS:
        return "cuenta: cuello del sistema"
    return "fuera: faltó k6"


def criterio_c4(reacciones: Iterable[Reaccion]) -> str:
    """C4 de una etapa: los servicios en ALARM escalaron, o estaban al tope."""
    lista = list(reacciones)
    if not lista:
        return "sin datos"
    sin_escalar = [r.servicio for r in lista if r.alarma is not None and r.actividad is None]
    if sin_escalar:
        return f"no cumple: {', '.join(sin_escalar)} en ALARM sin escalar"
    escalaron = [r.servicio for r in lista if r.actividad is not None]
    al_tope = [r.servicio for r in lista if r.nota.startswith("al tope")]
    partes = ([f"escaló {', '.join(escalaron)}"] if escalaron else []) + (
        [f"al tope: {', '.join(al_tope)}"] if al_tope else []
    )
    if escalaron:
        return f"cumple: {'; '.join(partes)}"
    return "; ".join(partes) or "sin escalado requerido"


def evaluar(
    etapas: Iterable[dict[str, Any]],
    metricas: dict[str, dict[str, Any]],
    reacciones: Iterable[Reaccion],
) -> list[dict[str, Any]]:
    """Evaluación preliminar por etapa con la regla de etapa válida y C2–C4 del manifiesto. C1
    (p95 de cotizacion ≤ 250 ms) se juzga en el informe (9.I) con la tabla de CloudWatch."""
    lista = list(reacciones)
    filas = []
    for etapa in etapas:
        nombre = str(etapa["etapa"])
        valida = validez(etapa)
        pedida, lograda = etapa.get("pedidaPorMinuto"), etapa.get("logradaPorMinuto")
        tasa = lograda / pedida if pedida and lograda is not None else None
        error_k6 = etapa.get("tasaError")
        datos = metricas.get(nombre) or {}
        n, errores = datos.get("cotizacion.n"), datos.get("cotizacion.errores")
        error_cotizacion = (errores or 0) / n if n else None
        filas.append(
            {
                "etapa": nombre,
                "validez": valida,
                "cuenta": not valida.startswith("fuera"),
                "tasaLograda": None if tasa is None else round(tasa, 3),
                "c2": None if tasa is None else tasa >= META_TASA,
                "errorK6": error_k6,
                "errorCotizacion": None if error_cotizacion is None else round(error_cotizacion, 4),
                "c3": None
                if error_k6 is None
                else (
                    error_k6 <= META_ERRORES
                    and (error_cotizacion is None or error_cotizacion <= META_ERRORES)
                ),
                "c4": criterio_c4(r for r in lista if r.etapa == nombre),
            }
        )
    return filas


def datos_faltantes(recogida: dict[str, Any]) -> list[str]:
    """Lo que falta para la conclusión: lo único que hace fallar a hu_w09_recoger."""
    faltan = []
    inicio = recogida.get("inicio") or {}
    if not inicio.get("t0"):
        faltan.append(f"la línea {PREFIJO_INICIO} con t0")
    etapas = recogida.get("etapas") or []
    if not etapas:
        faltan.append(f"las líneas {PREFIJO_ETAPA}")
        return faltan
    recibidas = {e.get("etapa") for e in etapas}
    perdidas = [
        str(e.get("etapa")) for e in inicio.get("etapas") or [] if e.get("etapa") not in recibidas
    ]
    if perdidas:
        faltan.append(f"las líneas {PREFIJO_ETAPA} de {', '.join(perdidas)}")
    metricas = recogida.get("metricas") or {}
    if all((metricas.get(str(e.get("etapa"))) or {}).get("cotizacion.p95") is None for e in etapas):
        faltan.append("OperationDuration de cotizacion en todas las etapas")
    return faltan


def limitar(texto: str, limite: int = LIMITE_RESUMEN) -> str:
    """El texto, recortado en un fin de línea si pasa de `limite` bytes, con una nota al final."""
    datos = texto.encode()
    if len(datos) <= limite:
        return texto
    nota = f"\n\n{NOTA_RECORTE}"
    recorte = datos[: limite - len(nota.encode())].decode(errors="ignore")
    if "\n" in recorte:
        recorte = recorte[: recorte.rfind("\n")]
    return recorte + nota


def _celda(valor: Any) -> str:
    if valor is None or valor == "":
        return "—"
    if isinstance(valor, bool):
        return "sí" if valor else "no"
    if isinstance(valor, dict) and set(valor) == {"min", "max"}:
        return f"{_celda(valor['min'])}–{_celda(valor['max'])}"
    if isinstance(valor, float):
        return f"{valor:g}" if abs(valor) < 1e6 else f"{valor:.0f}"
    return str(valor).replace("|", "\\|").replace("\n", " ")


def _tabla(encabezados: list[str], filas: Iterable[Iterable[Any]]) -> str:
    lineas = [f"| {' | '.join(encabezados)} |", f"|{'---|' * len(encabezados)}"]
    lineas += [f"| {' | '.join(_celda(c) for c in fila)} |" for fila in filas]
    return "\n".join(lineas)


def _hora(valor: Any) -> str | None:
    return str(valor)[11:19] if valor else None


def resumen_operacion(
    titulo: str, datos: list[tuple[str, Any]], cadena: list[dict[str, Any]] | None = None
) -> str:
    """El resumen de preparar, lanzar o detener: una tabla de datos y, si hay, la cadena."""
    partes = [f"### {HISTORIA} · {titulo}", _tabla(["Dato", "Valor"], datos)]
    if cadena:
        partes += [
            "#### Cadena",
            _tabla(
                ["Servicio", "Estado", "Deseadas", "Corriendo", "Despliegues", "Estable"],
                [
                    [
                        f["servicio"],
                        f["estado"],
                        f["deseadas"],
                        f["corriendo"],
                        f["despliegues"],
                        f["estable"],
                    ]
                    for f in cadena
                ],
            ),
        ]
    return limitar("\n\n".join(partes))


def renderizar_resumen(recogida: dict[str, Any]) -> str:
    """Las tablas Markdown de hu_w09_recoger para $GITHUB_STEP_SUMMARY (menos de 1 MiB)."""
    tarea = recogida.get("tarea") or {}
    inicio = recogida.get("inicio") or {}
    etapas = recogida.get("etapas") or []
    metricas = recogida.get("metricas") or {}
    nombres = [str(e.get("etapa")) for e in etapas]
    ventanas_por = {v["etapa"]: v for v in recogida.get("ventanas") or []}

    def m(etapa: str, clave: str) -> Any:
        return (metricas.get(etapa) or {}).get(clave)

    def par(etapa: str, clave: str) -> str:
        return f"{_celda(m(etapa, f'{clave}.prom'))} / {_celda(m(etapa, f'{clave}.max'))}"

    def ventana(etapa: str) -> str | None:
        v = ventanas_por.get(etapa) or {}
        return f"{str(v['desde'])[11:16]}–{str(v['hasta'])[11:16]}" if v.get("desde") else None

    caches = sorted(
        {c.split(".")[1] for datos in metricas.values() for c in datos if c.startswith("valkey.")}
    )
    columnas_cw = [
        ("cotizacion p95 ms", "cotizacion.p95"),
        ("p99 ms", "cotizacion.p99"),
        ("n", "cotizacion.n"),
        ("errores", "cotizacion.errores"),
        ("socio.cotizar p95 ms", "socio.p95"),
        ("p99 ms", "socio.p99"),
        ("ALB solicitudes", "alb.solicitudes"),
        ("ALB p95 ms", "alb.p95"),
        ("ALB p99 ms", "alb.p99"),
        ("5xx destino", "alb.5xx_destino"),
        ("5xx ALB", "alb.5xx_elb"),
    ]
    # HU-W09-TOKENS solo existe con el tramo por API Gateway. sin_token llegó después que fallidos:
    # una línea sin ese campo muestra «sin dato» en vez de fallar.
    linea_tokens = recogida.get("tokens")
    tokens = (linea_tokens or {}).get("fallidos")
    if linea_tokens is None:
        sin_token: Any = None
    else:
        sin_token = linea_tokens.get("sin_token", "sin dato")
    partes = [
        f"### {HISTORIA} · recoger",
        (
            f"- Tarea k6 `{tarea.get('id')}`: {_celda(tarea.get('estado'))}, exitCode "
            f"{_celda(tarea.get('exitCode'))}, {_celda(tarea.get('stoppedReason'))}\n"
            f"- t0 ({PREFIJO_INICIO}): {_celda(inicio.get('t0'))} · tramos: "
            f"{', '.join(inicio.get('tramos') or []) or '—'} · tokens fallidos: {_celda(tokens)}"
            f" · iteraciones sin token: {_celda(sin_token)}"
        ),
        "#### k6 por etapa",
        _tabla(
            [
                "Etapa",
                "Tramo",
                "Pedida/min",
                "Lograda/min",
                "Solicitudes",
                "Errores",
                "Tasa de error",
                "4xx/5xx/red",
                "p95 ms",
                "p99 ms",
                "dropped",
            ],
            [
                [
                    e.get("etapa"),
                    e.get("tramo"),
                    e.get("pedidaPorMinuto"),
                    e.get("logradaPorMinuto"),
                    e.get("solicitudes"),
                    e.get("errores"),
                    e.get("tasaError"),
                    f"{e.get('errores4xx')}/{e.get('errores5xx')}/{e.get('erroresRed')}",
                    e.get("p95Ms"),
                    e.get("p99Ms"),
                    e.get("dropped"),
                ]
                for e in etapas
            ],
        ),
        "#### CloudWatch por etapa (sin el primer minuto)",
        _tabla(
            ["Etapa", "Ventana (UTC)"] + [t for t, _ in columnas_cw],
            [[n, ventana(n)] + [m(n, c) for _, c in columnas_cw] for n in nombres],
        ),
        "#### Tareas por servicio (mínimo–máximo por minuto)",
        _tabla(
            ["Etapa", *SERVICIOS], [[n] + [m(n, f"tareas.{s}") for s in SERVICIOS] for n in nombres]
        ),
        "#### Recursos: CPU de ECS (%, promedio / máximo)",
        _tabla(
            ["Etapa", *SERVICIOS], [[n] + [par(n, f"cpu.{s}") for s in SERVICIOS] for n in nombres]
        ),
        "#### Recursos: memoria de ECS (%, promedio / máximo)",
        _tabla(
            ["Etapa", *SERVICIOS], [[n] + [par(n, f"mem.{s}") for s in SERVICIOS] for n in nombres]
        ),
        "#### Recursos: datos (promedio / máximo)",
        _tabla(
            ["Etapa", "RDS CPU %", "RDS conexiones"] + [f"Valkey {c} CPU %" for c in caches],
            [
                [n, par(n, "rds.cpu"), par(n, "rds.conexiones")]
                + [par(n, f"valkey.{c}.cpu") for c in caches]
                for n in nombres
            ],
        ),
        "#### Reacción del autoescalado (segundos)",
        _tabla(
            [
                "Etapa",
                "Servicio",
                "ALARM (UTC)",
                "Detección",
                "Decisión",
                "Capacidad",
                "Total",
                "Nota",
            ],
            [
                [
                    r.get("etapa"),
                    r.get("servicio"),
                    _hora(r.get("alarma")),
                    r.get("deteccion_s"),
                    r.get("decision_s"),
                    r.get("capacidad_s"),
                    r.get("total_s"),
                    r.get("nota"),
                ]
                for r in recogida.get("reacciones") or []
            ],
        ),
        "#### Línea de tiempo de tareas (minuto desde t0: deseadas/corriendo)",
        _tabla(
            ["Servicio", "Cambios"],
            [
                [s, " · ".join(f"{t:g}′ {d}/{_celda(c)}" for t, d, c in puntos)]
                for s, puntos in (recogida.get("lineas_de_tiempo") or {}).items()
            ],
        ),
        "#### Imágenes",
        _tabla(
            ["Servicio", "Contenedor", "Imagen", "Digest"],
            [
                [s, i.get("contenedor"), i.get("imagen"), i.get("digest")]
                for s, lista in (recogida.get("imagenes") or {}).items()
                for i in lista
            ],
        ),
        "#### Script de k6 (revisión que corrió)",
        _tabla(
            ["Revisión", "sha256", "sha256 del commit", "Coincide", "Imagen", "Digest", "Nota"],
            [
                [
                    script.get("revision"),
                    script.get("sha256"),
                    script.get("sha256Commit"),
                    script.get("coincide"),
                    script.get("imagen"),
                    script.get("digest"),
                    script.get("nota"),
                ]
            ]
            if (script := recogida.get("script") or {})
            else [],
        ),
        "#### Evaluación preliminar (manifiesto §1; no es la conclusión)",
        (
            "C2: lograda ≥ 95 % de la pedida. C3: errores ≤ 1 % (k6 y `OperationErrors` de "
            "cotizacion). C4: los servicios en ALARM escalaron o estaban al tope. C1 (p95 de "
            "cotizacion ≤ 250 ms) se juzga en el informe con la tabla de CloudWatch. C2–C4 siguen "
            "siendo la propuesta de la 9.3."
        ),
        _tabla(
            [
                "Etapa",
                "Validez",
                "Lograda/pedida",
                "C2",
                "Error k6",
                "Error cotizacion",
                "C3",
                "C4",
            ],
            [
                [
                    f["etapa"],
                    f["validez"],
                    f["tasaLograda"],
                    f["c2"],
                    f["errorK6"],
                    f["errorCotizacion"],
                    f["c3"],
                    f["c4"],
                ]
                for f in recogida.get("evaluacion") or []
            ],
        ),
        "#### Avisos",
        "\n".join(f"- {_celda(a)}" for a in recogida.get("avisos") or []) or "- ninguno",
    ]
    return limitar("\n\n".join(partes))


def _iso(valor: Any) -> str | None:
    return instante(valor).isoformat(timespec="seconds") if valor else None


def _reaccion_dict(r: Reaccion) -> dict[str, Any]:
    return {
        "etapa": r.etapa,
        "servicio": r.servicio,
        "alarma": _iso(r.alarma),
        "actividad": _iso(r.actividad),
        "capacidad": _iso(r.capacidad),
        "deteccion_s": r.deteccion_s,
        "decision_s": r.decision_s,
        "capacidad_s": r.capacidad_s,
        "total_s": r.total_s,
        "nota": r.nota,
    }


# --- Guardas y AWS -------------------------------------------------------------------------


def _requerido(motivo: str) -> NoReturn:
    """Falta de configuración o precondición: falla con E2E_REQUERIDO=1, si no se omite."""
    if os.getenv("E2E_REQUERIDO") == "1":
        pytest.fail(motivo)
    pytest.skip(motivo)


@dataclass(frozen=True)
class Aws:
    ecs: Any
    ec2: Any
    elbv2: Any
    logs: Any
    apigateway: Any
    cognito: Any
    sns: Any
    cloudwatch: Any
    autoscaling: Any
    servicequotas: Any


@dataclass(frozen=True)
class Operacion:
    nombre: str
    contexto: Contexto
    aws: Aws
    secretos: Secretos

    def uno(
        self, candidatos: Iterable[Any], que: str, describir: Callable[[Any], str] = str
    ) -> Any:
        try:
            return unico(candidatos, que, describir)
        except (LookupError, ValueError) as error:
            self.secretos.fallar(f"{HISTORIA}: {error}")


def _emisor(request: pytest.FixtureRequest) -> Callable[[str], None]:
    """Escribe fuera de la captura de pytest, para que GitHub procese `::add-mask::` en el momento."""
    captura = request.config.pluginmanager.getplugin("capturemanager")

    def emitir(linea: str) -> None:
        if captura is None:
            print(linea, flush=True)
            return
        with captura.global_and_fixture_disabled():
            sys.stdout.write(f"{linea}\n")
            sys.stdout.flush()

    return emitir


def _clientes_aws(region: str) -> Aws:
    try:
        import boto3
    except ImportError:  # pragma: no cover
        _requerido(f"N3 {HISTORIA}: boto3 no está instalado")
    try:
        sesion = boto3.session.Session(region_name=region)
        sesion.client("sts").get_caller_identity()
    except (BotoCoreError, ClientError) as error:
        _requerido(f"N3 {HISTORIA}: sin acceso a AWS ({type(error).__name__})")
    cliente = sesion.client
    return Aws(
        ecs=cliente("ecs"),
        ec2=cliente("ec2"),
        elbv2=cliente("elbv2"),
        logs=cliente("logs"),
        apigateway=cliente("apigateway"),
        cognito=cliente("cognito-idp"),
        sns=cliente("sns"),
        cloudwatch=cliente("cloudwatch"),
        autoscaling=cliente("application-autoscaling"),
        servicequotas=cliente("service-quotas"),
    )


def _abrir(request: pytest.FixtureRequest, nombre: str) -> Operacion:
    """Las guardas, en orden, antes de tocar AWS."""
    if not seleccionada(request.config.getoption("keyword"), nombre):
        pytest.skip(f"{HISTORIA}: {MOTIVOS[nombre]}; selecciónala con -k {nombre}")
    if os.getenv("ENV") != AMBIENTE:
        pytest.skip(f"{HISTORIA}: {nombre} solo corre con ENV=exp (ENV={os.getenv('ENV')!r})")
    try:
        contexto = contexto_de(request.getfixturevalue("ambiente").salidas)
    except ValueError as error:
        _requerido(f"N3 {HISTORIA}: {error}")
    aws = _clientes_aws(contexto.region)
    secretos = Secretos(_emisor(request), os.getenv("GITHUB_ACTIONS") == "true")
    return Operacion(nombre, contexto, aws, secretos)


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


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _cadena(op: Operacion) -> list[dict[str, Any]]:
    return estado_de_la_cadena(
        op.aws.ecs.describe_services(cluster=op.contexto.cluster, services=list(SERVICIOS))
    )


def _tareas(op: Operacion, estado: str, iniciada_por: str | None = None) -> list[dict[str, Any]]:
    parametros: dict[str, Any] = {"cluster": op.contexto.cluster, "desiredStatus": estado}
    if iniciada_por:
        parametros["startedBy"] = iniciada_por
    arns = _paginar(op.aws.ecs.list_tasks, "taskArns", token="nextToken", **parametros)
    tareas: list[dict[str, Any]] = []
    for i in range(0, len(arns), 100):
        tareas += op.aws.ecs.describe_tasks(
            cluster=op.contexto.cluster, tasks=arns[i : i + 100]
        ).get("tasks", [])
    return tareas


def _resumen_tareas(tareas: Iterable[dict[str, Any]]) -> list[list[Any]]:
    """Id, startedBy, estado y creación: nunca la tarea entera (trae containerOverrides)."""
    return [
        [
            t["taskArn"].rsplit("/", 1)[-1],
            t.get("startedBy"),
            t.get("lastStatus"),
            _iso(t.get("createdAt")),
        ]
        for t in tareas
    ]


def _url_alb(op: Operacion) -> tuple[str, str]:
    nombre = f"{op.contexto.prefijo}-access"
    try:
        albs = op.aws.elbv2.describe_load_balancers(Names=[nombre]).get("LoadBalancers", [])
    except ClientError as error:
        op.secretos.fallar(f"{HISTORIA}: no se encontró el ALB {nombre} ({_codigo(error)})")
    alb = op.uno(
        [a for a in albs if a.get("Scheme") == "internal"],
        f"ALB interno {nombre}",
        lambda a: str(a.get("LoadBalancerName")),
    )
    origen = (
        f"DNS de describe_load_balancers del ALB {nombre} (infra/platform/edge.tf:43); puerto "
        f"{op.contexto.puerto_alb}, el listener_port de api-socios en la salida platform.catalog "
        "(infra/platform/catalog.tf:36)"
    )
    return f"http://{alb['DNSName']}:{op.contexto.puerto_alb}", origen


def _sg_k6(op: Operacion) -> str:
    nombre = f"{op.contexto.prefijo}-carga"
    grupos = op.aws.ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [nombre]},
            {"Name": "vpc-id", "Values": [op.contexto.vpc]},
        ]
    ).get("SecurityGroups", [])
    return str(op.uno(grupos, f"SG {nombre} en la VPC", lambda g: str(g.get("GroupId")))["GroupId"])


def _lanzar_k6(
    op: Operacion, tarea: str, iniciada_por: str, variables: dict[str, str]
) -> tuple[str, dict[str, Any]]:
    """Lanza la definición <prefijo>-<familia> de `tarea` (alta o carga; el script ya viaja en
    ella) en FARGATE, sin IP pública, con solo variables de entorno en containerOverrides."""
    contexto, aws, secretos = op.contexto, op.aws, op.secretos
    secretos.agregar(*valores_secretos(variables))
    familia = f"{contexto.prefijo}-{FAMILIAS[tarea]}"
    try:
        definicion = aws.ecs.describe_task_definition(taskDefinition=familia)["taskDefinition"]
    except ClientError as error:
        secretos.fallar(
            f"{HISTORIA}: no existe la definición de tarea {familia} ({_codigo(error)}): "
            "¿load_test_enabled = true en envs/exp/apps.tfvars?"
        )
    try:
        overrides = sobrescrituras(variables)
    except ValueError as error:
        secretos.fallar(f"{HISTORIA}: {error}")
    respuesta = aws.ecs.run_task(
        cluster=contexto.cluster,
        taskDefinition=definicion["taskDefinitionArn"],
        launchType="FARGATE",
        count=1,
        startedBy=iniciada_por,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": list(contexto.subredes),
                "securityGroups": [_sg_k6(op)],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides=overrides,
    )
    fallas = respuesta.get("failures") or []
    if fallas or not respuesta.get("tasks"):
        secretos.fallar(
            f"{HISTORIA}: run_task no lanzó {definicion['taskDefinitionArn']}: "
            f"{[(f.get('reason'), f.get('detail')) for f in fallas]}"
        )
    return str(respuesta["tasks"][0]["taskArn"]), definicion


def _script_local(tarea: str) -> bytes | None:
    ruta = DIR_SCRIPTS / f"{tarea}.js"
    return ruta.read_bytes() if ruta.is_file() else None


def _cuotas_vcpu(op: Operacion) -> dict[str, dict[str, Any]]:
    """Las cuotas de vCPU de Fargate por Service Quotas (solo lectura): la aplicada o, si la
    cuenta no tiene una propia, la de AWS por omisión. Si no se puede leer, lo dice; nunca aborta."""
    cuotas = {}
    for codigo, nombre in CUOTAS_VCPU.items():
        try:
            try:
                respuesta = op.aws.servicequotas.get_service_quota(
                    ServiceCode="fargate", QuotaCode=codigo
                )
                origen = "aplicada"
            except ClientError as error:
                if _codigo(error) != "NoSuchResourceException":
                    raise
                respuesta = op.aws.servicequotas.get_aws_default_service_quota(
                    ServiceCode="fargate", QuotaCode=codigo
                )
                origen = "por omisión de AWS"
            cuotas[codigo] = {
                "nombre": nombre,
                "valor": float(respuesta["Quota"]["Value"]),
                "origen": origen,
            }
        except (BotoCoreError, ClientError, KeyError, TypeError, ValueError) as error:
            cuotas[codigo] = {
                "nombre": nombre,
                "valor": None,
                "origen": f"no se pudo leer ({_codigo(error)})",
            }
    return cuotas


def _necesidad_vcpu(op: Operacion) -> dict[str, Any]:
    """CPU de la definición vigente, desiredCount y MaxCapacity de cada servicio de la cadena, y
    la CPU de la tarea k6 de carga. Lo que no se pueda leer queda en `faltan`."""
    contexto, aws = op.contexto, op.aws
    servicios: list[dict[str, Any]] = []
    try:
        descritos = {
            s["serviceName"]: s
            for s in aws.ecs.describe_services(
                cluster=contexto.cluster, services=list(SERVICIOS)
            ).get("services", [])
        }
        objetivos = {
            o["ResourceId"].rsplit("/", 1)[-1]: o
            for o in aws.autoscaling.describe_scalable_targets(
                ServiceNamespace="ecs",
                ResourceIds=[f"service/{contexto.cluster}/{s}" for s in SERVICIOS],
                ScalableDimension=DIMENSION,
            ).get("ScalableTargets", [])
        }
    except (BotoCoreError, ClientError) as error:
        print(
            f"{HISTORIA} preparar: AVISO, no se pudo leer la cadena para las vCPU ({_codigo(error)})"
        )
        descritos, objetivos = {}, {}
    for servicio in SERVICIOS:
        descrito = descritos.get(servicio) or {}
        cpu = None
        if descrito.get("taskDefinition"):
            try:
                cpu = aws.ecs.describe_task_definition(taskDefinition=descrito["taskDefinition"])[
                    "taskDefinition"
                ].get("cpu")
            except (BotoCoreError, ClientError):
                cpu = None
        deseadas = descrito.get("desiredCount")
        maximo = (objetivos.get(servicio) or {}).get("MaxCapacity", deseadas)
        servicios.append({"servicio": servicio, "cpu": cpu, "deseadas": deseadas, "maximo": maximo})
    try:
        k6 = aws.ecs.describe_task_definition(
            taskDefinition=f"{contexto.prefijo}-{FAMILIAS['carga']}"
        )["taskDefinition"].get("cpu")
    except (BotoCoreError, ClientError):
        k6 = None
    return necesidad_vcpu(servicios, k6)


def _esperar(
    op: Operacion, arn: str, listo: Callable[[dict[str, Any]], bool], espera_s: float
) -> dict[str, Any]:
    limite = time.monotonic() + espera_s
    while True:
        tareas = (
            op.aws.ecs.describe_tasks(cluster=op.contexto.cluster, tasks=[arn]).get("tasks") or []
        )
        tarea = tareas[0] if tareas else {}
        if listo(tarea) or time.monotonic() >= limite:
            return tarea
        time.sleep(MUESTREO_S)


def _eventos_de_log(op: Operacion, flujo: str) -> list[str]:
    grupo = f"/ecs/{op.contexto.prefijo}/carga"  # infra/apps/carga.tf:18
    lineas: list[str] = []
    token = None
    for _ in range(1000):
        parametros: dict[str, Any] = {
            "logGroupName": grupo,
            "logStreamName": flujo,
            "startFromHead": True,
        }
        if token:
            parametros["nextToken"] = token
        try:
            respuesta = op.aws.logs.get_log_events(**parametros)
        except ClientError as error:
            if _codigo(error) == "ResourceNotFoundException":
                return lineas
            raise
        lineas.extend(str(e.get("message", "")) for e in respuesta.get("events", []))
        if respuesta.get("nextForwardToken") == token:
            break
        token = respuesta.get("nextForwardToken")
    return lineas


def _log_k6(
    op: Operacion, arn: str, hasta: Callable[[list[str]], bool] | None = None, espera_s: float = 0
) -> list[str]:
    limite = time.monotonic() + espera_s
    while True:
        lineas = _eventos_de_log(op, flujo_k6(arn))
        if hasta is None or hasta(lineas) or time.monotonic() >= limite:
            return lineas
        time.sleep(MUESTREO_S)


def _contiene(prefijo: str) -> Callable[[list[str]], bool]:
    return lambda lineas: any(f"{prefijo} " in linea for linea in lineas)


def _flujo_mas_reciente(op: Operacion) -> tuple[str | None, list[str]]:
    """Si ECS ya olvidó la tarea: el flujo más reciente del log de k6 con HU-W09-INICIO."""
    flujos = op.aws.logs.describe_log_streams(
        logGroupName=f"/ecs/{op.contexto.prefijo}/carga",
        orderBy="LastEventTime",
        descending=True,
        limit=10,
    ).get("logStreams", [])
    for flujo in flujos:
        lineas = _eventos_de_log(op, flujo["logStreamName"])
        if _contiene(PREFIJO_INICIO)(lineas):
            return flujo["logStreamName"].rsplit("/", 1)[-1], lineas
    return None, []


def _claves(op: Operacion, consulta: str) -> list[dict[str, Any]]:
    return _paginar(
        op.aws.apigateway.get_api_keys,
        "items",
        token="position",
        nameQuery=consulta,
        includeValues=False,
        limit=500,
    )


def _plan_de(op: Operacion, key_id: str) -> str:
    claves = {
        nombre: [
            k["id"]
            for k in _paginar(
                op.aws.apigateway.get_usage_plan_keys,
                "items",
                token="position",
                usagePlanId=plan_id,
                limit=500,
            )
        ]
        for nombre, plan_id in op.contexto.planes.items()
    }
    return plan_efectivo(key_id, claves)


def _mover_a_experimento(op: Operacion, key_id: str) -> str:
    """Decisión 2: la API key de estandar a experimento. Si falla, no intenta otra vía: la deja
    (o la devuelve) en estandar y lo dice."""
    estandar, experimento = (
        op.contexto.planes.get("estandar"),
        op.contexto.planes.get("experimento"),
    )
    sigue = "el tramo gw irá en estandar (decisión 2, opción c). No se intentó otra vía"
    if not experimento:
        return f"no se movió: no hay plan experimento en platform.usage_plans; {sigue}"
    apigateway = op.aws.apigateway
    devuelta: bool | None = None
    try:
        try:
            apigateway.delete_usage_plan_key(usagePlanId=estandar, keyId=key_id)
        except ClientError as error:
            if _codigo(error) != "NotFoundException":
                raise
        try:
            apigateway.create_usage_plan_key(
                usagePlanId=experimento, keyId=key_id, keyType="API_KEY"
            )
        except ClientError:
            try:
                apigateway.create_usage_plan_key(
                    usagePlanId=estandar, keyId=key_id, keyType="API_KEY"
                )
                devuelta = True
            except ClientError:
                devuelta = False
            raise
    except ClientError as error:
        donde = {
            None: "sigue en estandar",
            True: "se devolvió a estandar",
            False: "AVISO: quedó sin plan y no se pudo devolver a estandar",
        }[devuelta]
        return f"no se movió ({_codigo(error)}); {donde}; {sigue}"
    return "movida de estandar a experimento (delete_usage_plan_key y create_usage_plan_key)"


def _metricas(
    op: Operacion, consultas: list[Consulta], desde: datetime, hasta: datetime
) -> list[dict[str, Any]]:
    return _paginar(
        op.aws.cloudwatch.get_metric_data,
        "MetricDataResults",
        MetricDataQueries=a_consultas_aws(consultas),
        StartTime=desde,
        EndTime=hasta,
        ScanBy="TimestampAscending",
    )


def _autoescalado(
    op: Operacion, t0: datetime, vs: list[Ventana], avisos: list[str]
) -> tuple[list[Reaccion], dict[str, list[list[Any]]]]:
    """Por servicio: scalable target, alarma alta de su política, historial, actividades y tareas
    nuevas (events_log_group); la reacción por etapa y la línea de tiempo de tareas."""
    contexto, aws = op.contexto, op.aws
    fin = max(v.fin for v in vs)
    desde = t0 - timedelta(minutes=30)
    eventos = []
    for evento in _paginar(
        aws.logs.filter_log_events,
        "events",
        token="nextToken",
        logGroupName=contexto.eventos,
        startTime=_ms(t0 - timedelta(minutes=5)),
        endTime=_ms(fin + timedelta(minutes=10)),
        filterPattern='{ $.detail.lastStatus = "RUNNING" }',
    ):
        try:
            eventos.append(json.loads(evento.get("message", "")))
        except ValueError:
            continue
    en_marcha = tareas_en_marcha(eventos)
    consultas = consultas_de_tareas(contexto.medicion)
    series = resumir(consultas, _metricas(op, consultas, _minuto_abajo(t0), _minuto_arriba(fin)))
    reacciones: list[Reaccion] = []
    lineas: dict[str, list[list[Any]]] = {}
    for servicio in SERVICIOS:
        recurso = f"service/{contexto.cluster}/{servicio}"
        objetivos = aws.autoscaling.describe_scalable_targets(
            ServiceNamespace="ecs",
            ResourceIds=[recurso],
            ScalableDimension=DIMENSION,
        ).get("ScalableTargets", [])
        if len(objetivos) != 1:
            avisos.append(f"{servicio}: {len(objetivos)} scalable targets; no se mide su reacción")
            continue
        minimo, maximo = int(objetivos[0]["MinCapacity"]), int(objetivos[0]["MaxCapacity"])
        nombre = f"{contexto.prefijo}-{servicio}-cpu"
        try:
            politica: Politica | None = politica_de(
                aws.autoscaling.describe_scaling_policies(
                    ServiceNamespace="ecs",
                    ResourceId=recurso,
                    ScalableDimension=DIMENSION,
                    PolicyNames=[nombre],
                ),
                nombre,
            )
        except (LookupError, ValueError) as error:
            avisos.append(f"{servicio}: {error}")
            politica = None
        actividades = [
            a
            for a in actividades_de(
                _paginar(
                    aws.autoscaling.describe_scaling_activities,
                    "ScalingActivities",
                    paginas=10,
                    ServiceNamespace="ecs",
                    ResourceId=recurso,
                    ScalableDimension=DIMENSION,
                    MaxResults=50,
                )
            )
            if a.inicio >= desde
        ]
        if politica is None:
            reacciones += [
                Reaccion(v.etapa, servicio, nota="sin política de CPU: no se mide") for v in vs
            ]
        else:
            cambios = cambios_de_alarma(
                _paginar(
                    aws.cloudwatch.describe_alarm_history,
                    "AlarmHistoryItems",
                    AlarmName=politica.alarma_alta,
                    HistoryItemType="StateUpdate",
                    StartDate=desde,
                    EndDate=fin + timedelta(minutes=10),
                    ScanBy="TimestampAscending",
                    MaxRecords=100,
                )
            )
            reacciones += [
                reaccion(
                    v, servicio, cambios, actividades, en_marcha.get(servicio, []), minimo, maximo
                )
                for v in vs
            ]
        serie = [(instante(t), valor) for t, valor in series.get(servicio) or []]
        lineas[servicio] = [
            [m.t, m.deseadas, m.corriendo]
            for m in linea_de_tiempo(muestras_de(serie, actividades, minimo, t0))
        ]
    return reacciones, lineas


def _imagenes(op: Operacion, tarea_k6: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """Datos del manifiesto: imagen y digest de las definiciones vigentes de la cadena y de k6."""
    contexto, ecs = op.contexto, op.aws.ecs
    imagenes: dict[str, list[dict[str, Any]]] = {}
    for servicio in ecs.describe_services(cluster=contexto.cluster, services=list(SERVICIOS)).get(
        "services", []
    ):
        definicion = ecs.describe_task_definition(taskDefinition=servicio["taskDefinition"])[
            "taskDefinition"
        ]
        arns = ecs.list_tasks(
            cluster=contexto.cluster, serviceName=servicio["serviceName"], desiredStatus="RUNNING"
        ).get("taskArns", [])[:1]
        tareas = (
            ecs.describe_tasks(cluster=contexto.cluster, tasks=arns).get("tasks", [])
            if arns
            else []
        )
        imagenes[servicio["serviceName"]] = imagenes_de(definicion, tareas[0] if tareas else None)
    if tarea_k6 is not None:
        definicion = ecs.describe_task_definition(taskDefinition=tarea_k6["taskDefinitionArn"])[
            "taskDefinition"
        ]
        imagenes["k6"] = imagenes_de(definicion, tarea_k6)
    return imagenes


# --- Las cuatro pruebas --------------------------------------------------------------------


def test_hu_w09_preparar(request: pytest.FixtureRequest) -> None:
    op = _abrir(request, "hu_w09_preparar")
    contexto, aws, secretos = op.contexto, op.aws, op.secretos

    # Cadena estable y suscripción de alertas (decisión 11: solo se informa).
    cadena = _cadena(op)
    suscripcion = estado_suscripciones(
        _paginar(
            aws.sns.list_subscriptions_by_topic, "Subscriptions", TopicArn=contexto.tema_alertas
        )
    )
    secretos.imprimir(
        f"\n{HISTORIA} preparar: cadena {json.dumps(cadena)}; "
        f"suscripción de alertas: {suscripcion['estado']}"
    )
    inestables = [f["servicio"] for f in cadena if not f["estable"]]
    if inestables:
        secretos.resumen(
            resumen_operacion(
                "preparar", [("Suscripción de alertas", suscripcion["estado"])], cadena
            )
        )
        _requerido(
            f"N3 {HISTORIA}: la cadena no está estable ({', '.join(inestables)}); "
            "espera a que terminen los despliegues"
        )

    # Cuotas de vCPU de Fargate frente a lo que pide la cadena al máximo con k6 (solo se informa).
    cuotas = filas_de_cuotas(_cuotas_vcpu(op), _necesidad_vcpu(op))
    secretos.imprimir(
        f"{HISTORIA} preparar: vCPU de Fargate {json.dumps(dict(cuotas), ensure_ascii=False)}"
    )

    # Alta interna del socio con alta.js (decisión 3), desde la definición <prefijo>-carga-alta.
    alb_url, origen = _url_alb(op)
    arn, _ = _lanzar_k6(op, "alta", INICIADA_POR_ALTA, {"ALB_URL": alb_url})
    secretos.imprimir(f"{HISTORIA} preparar: tarea del alta {arn}")
    tarea = _esperar(op, arn, lambda t: t.get("lastStatus") == "STOPPED", ESPERA_ALTA_S)
    if tarea.get("lastStatus") != "STOPPED":
        aws.ecs.stop_task(
            cluster=contexto.cluster, task=arn, reason="HU-W09: el alta no terminó a tiempo"
        )
    lineas = _log_k6(op, arn, hasta=_contiene(PREFIJO_ALTA), espera_s=ESPERA_LOG_S)
    try:
        socio, client_id = leer_alta(lineas)
        secretos.agregar(client_id)  # antes de imprimir cualquier línea del log
        error_alta = None
    except (LookupError, ValueError) as error:
        socio, error_alta = "", str(error)
    salida = salida_de(tarea)
    if salida["estado"] != "STOPPED" or salida["exitCode"] != 0 or error_alta:
        secretos.fallar(
            f"{HISTORIA}: el alta del socio no terminó bien ({json.dumps(salida)}"
            f"{f'; {error_alta}' if error_alta else ''}). Últimas líneas del log de k6:\n"
            + "\n".join(lineas[-20:])
        )

    # Su API key, por nombre y etiqueta, de estandar a experimento (decisión 2).
    nombre_clave = f"{contexto.prefijo}-socio-{socio}"
    clave = op.uno(
        [
            c
            for c in _claves(op, nombre_clave)
            if c.get("name") == nombre_clave and (c.get("tags") or {}).get("Socio") == socio
        ],
        f"API key {nombre_clave} con la etiqueta Socio={socio}",
        lambda c: str(c.get("name")),
    )
    key_id = str(clave["id"])
    secretos.agregar(key_id)
    movimiento = _mover_a_experimento(op, key_id)
    plan = _plan_de(op, key_id)

    datos = [
        ("Socio", socio),
        ("Plan efectivo de la API key", plan),
        ("Movimiento de plan", movimiento),
        (
            "Suscripción de alertas (decisión 11)",
            f"{suscripcion['estado']} ({', '.join(suscripcion['protocolos']) or '—'})",
        ),
        ("URL del ALB", f"{alb_url} — {origen}"),
        ("Tarea del alta", arn.rsplit("/", 1)[-1]),
        *cuotas,
    ]
    secretos.imprimir(
        f"{HISTORIA} preparar, resumen: {json.dumps(dict(datos), ensure_ascii=False)}"
    )
    secretos.resumen(resumen_operacion("preparar", datos, cadena))
    if plan != "experimento":
        secretos.imprimir(f"{HISTORIA} preparar: AVISO, la API key quedó en {plan}: {movimiento}")


def test_hu_w09_lanzar(request: pytest.FixtureRequest) -> None:
    op = _abrir(request, "hu_w09_lanzar")
    contexto, aws, secretos = op.contexto, op.aws, op.secretos

    vivas = tareas_hu_w09(_tareas(op, "RUNNING"))
    if vivas:
        secretos.fallar(
            f"{HISTORIA}: ya hay tareas de HU-W09 vivas (id, startedBy, estado, creada): "
            f"{_resumen_tareas(vivas)}. Espera a que terminen o lanza hu_w09_detener"
        )

    # El socio de la preparación: la API key más reciente de alta.js, su cliente de Cognito y los
    # valores que solo necesita el tramo gw. Todos se enmascaran antes de usarse.
    try:
        socio, key_id, otros = socio_mas_reciente(
            _claves(op, f"{contexto.prefijo}-socio-SOC-"), contexto.prefijo
        )
    except (LookupError, ValueError) as error:
        secretos.fallar(f"{HISTORIA}: {error}")
    secretos.agregar(key_id)
    api_key = str(aws.apigateway.get_api_key(apiKey=key_id, includeValue=True).get("value") or "")
    secretos.agregar(api_key)
    clientes = _paginar(
        aws.cognito.list_user_pool_clients,
        "UserPoolClients",
        UserPoolId=contexto.pool_socios,
        MaxResults=60,
    )
    cliente = op.uno(
        [c for c in clientes if c.get("ClientName") == f"socio-{socio}"],
        f"cliente de Cognito socio-{socio}",
        lambda c: str(c.get("ClientName")),
    )
    client_id = str(cliente["ClientId"])
    secretos.agregar(client_id)
    secreto = str(
        aws.cognito.describe_user_pool_client(UserPoolId=contexto.pool_socios, ClientId=client_id)[
            "UserPoolClient"
        ].get("ClientSecret")
        or ""
    )
    secretos.agregar(secreto)

    plan = _plan_de(op, key_id)
    alb_url, origen = _url_alb(op)
    gw = TramoGw(
        contexto.api_socios,
        contexto.token_url,
        scope_cotizar(contexto.scopes),
        contexto.mtls,
        secreto,
        api_key,
    )
    variables, nota = variables_de_carga(alb_url, client_id, key_id, plan, gw)
    cadena = _cadena(op)

    arn, definicion = _lanzar_k6(op, "carga", INICIADA_POR_CARGA, variables)
    lanzada = datetime.now(UTC)
    secretos.imprimir(
        f"\n{HISTORIA} lanzar: tarea {arn} lanzada a las {lanzada.isoformat(timespec='seconds')}"
    )
    tarea = _esperar(
        op, arn, lambda t: t.get("lastStatus") in ("RUNNING", "STOPPED"), ESPERA_ARRANQUE_S
    )
    espera = ESPERA_INICIO_S if tarea.get("lastStatus") == "RUNNING" else ESPERA_LOG_S
    lineas = _log_k6(op, arn, hasta=_contiene(PREFIJO_INICIO), espera_s=espera)
    tarea = _esperar(op, arn, lambda t: True, 0)
    try:
        inicio = leer_resultado_k6(lineas).inicio
    except ValueError:
        inicio = None
    if tarea.get("lastStatus") == "STOPPED":
        secretos.fallar(
            f"{HISTORIA}: la tarea k6 se detuvo al arrancar ({json.dumps(salida_de(tarea))}). "
            "Últimas líneas del log de k6:\n" + "\n".join(lineas[-20:])
        )

    t0 = instante(inicio["t0"]) if inicio else lanzada
    origen_t0 = (
        PREFIJO_INICIO if inicio else f"hora del run_task: k6 aún no imprimió {PREFIJO_INICIO}"
    )
    recoger = t0 + timedelta(minutes=MINUTOS_PARA_RECOGER)
    datos = [
        ("Tarea k6", arn),
        ("Definición", definicion["taskDefinitionArn"]),
        ("Estado", tarea.get("lastStatus")),
        (
            "t0 (UTC)",
            f"{t0.isoformat(timespec='seconds')} ({origen_t0})",
        ),
        ("Recoger desde (UTC)", recoger.isoformat(timespec="seconds")),
        ("Tramos", ", ".join((inicio or {}).get("tramos") or []) or "—"),
        ("Socio", socio + (f" (otros socios de HU-W09: {', '.join(otros)})" if otros else "")),
        ("Plan efectivo de la API key", plan),
        ("Tramo gw", nota),
        ("URL del ALB", f"{alb_url} — {origen}"),
    ]
    secretos.imprimir(f"{HISTORIA} lanzar, resumen: {json.dumps(dict(datos), ensure_ascii=False)}")
    secretos.resumen(resumen_operacion("lanzar", datos, cadena))


def test_hu_w09_recoger(request: pytest.FixtureRequest) -> None:
    op = _abrir(request, "hu_w09_recoger")
    contexto, secretos = op.contexto, op.secretos
    avisos: list[str] = []

    tareas = [
        t for estado in ("RUNNING", "STOPPED") for t in _tareas(op, estado, INICIADA_POR_CARGA)
    ]
    tarea = ultima(tareas_hu_w09(tareas, exacto=INICIADA_POR_CARGA))
    if tarea is not None and tarea.get("lastStatus") != "STOPPED":
        desde = instante(tarea.get("startedAt") or tarea["createdAt"]) + timedelta(
            minutes=MINUTOS_PARA_RECOGER
        )
        secretos.fallar(
            f"{HISTORIA}: la tarea k6 {tarea['taskArn'].rsplit('/', 1)[-1]} todavía corre "
            f"({tarea.get('lastStatus')}); recoge después de {desde.isoformat(timespec='seconds')}"
        )
    if tarea is not None:
        id_tarea = tarea["taskArn"].rsplit("/", 1)[-1]
        lineas = _log_k6(op, tarea["taskArn"])
        resumen_tarea = {
            "id": id_tarea,
            "definicion": tarea.get("taskDefinitionArn"),
            "creada": _iso(tarea.get("createdAt")),
            "iniciada": _iso(tarea.get("startedAt")),
            "detenida": _iso(tarea.get("stoppedAt")),
            **salida_de(tarea),
        }
        if resumen_tarea["exitCode"] != 0:
            avisos.append(
                f"k6 terminó con exitCode {resumen_tarea['exitCode']} ({tarea.get('stoppedReason')})"
            )
    else:
        id_flujo, lineas = _flujo_mas_reciente(op)
        if id_flujo is None:
            secretos.fallar(
                f"{HISTORIA}: no hay tarea con startedBy {INICIADA_POR_CARGA} en ECS ni un flujo "
                f"con {PREFIJO_INICIO} en /ecs/{contexto.prefijo}/carga: ¿corrió hu_w09_lanzar?"
            )
        resumen_tarea = {"id": id_flujo, "estado": "desconocido"}
        avisos.append(
            "ECS ya no muestra la tarea (conserva las detenidas cerca de 1 h): se usó el flujo de "
            f"log más reciente con {PREFIJO_INICIO}, sin exitCode"
        )
    try:
        resultado = leer_resultado_k6(lineas)
    except ValueError as error:
        secretos.fallar(
            f"{HISTORIA}: el log de k6 de {resumen_tarea['id']} no se puede leer: {error}"
        )

    t0 = instante(resultado.inicio["t0"]) if resultado.inicio else None
    vs = ventanas(t0, resultado.etapas) if t0 else []
    metricas: dict[str, dict[str, Any]] = {}
    for v in vs:
        if v.desde is None or v.hasta is None:
            metricas[v.etapa] = {}
            avisos.append(f"{v.etapa}: la etapa no deja un minuto completo para medir")
            continue
        consultas = consultas_de_etapa(contexto.medicion, int((v.hasta - v.desde).total_seconds()))
        metricas[v.etapa] = resumir(consultas, _metricas(op, consultas, v.desde, v.hasta))
    reacciones, lineas_de_tiempo = _autoescalado(op, t0, vs, avisos) if t0 and vs else ([], {})

    # Para el manifiesto: la revisión que corrió (o, sin tarea, la vigente) y su script.
    revision = (tarea or {}).get("taskDefinitionArn") or f"{contexto.prefijo}-{FAMILIAS['carga']}"
    if tarea is None:
        avisos.append("sin la tarea, el script se lee de la revisión vigente de la definición")
    try:
        definicion_k6 = op.aws.ecs.describe_task_definition(taskDefinition=revision)[
            "taskDefinition"
        ]
        script = huella_del_script(definicion_k6, tarea, _script_local("carga"))
    except (BotoCoreError, ClientError) as error:
        script = {
            "revision": revision.rsplit("/", 1)[-1],
            "nota": f"no se pudo leer ({_codigo(error)})",
        }
    if script.get("nota"):
        avisos.append(f"script de k6: {script['nota']}")

    recogida: dict[str, Any] = {
        "tarea": resumen_tarea,
        "inicio": resultado.inicio,
        "tokens": resultado.tokens,
        "etapas": resultado.etapas,
        "ventanas": [
            {
                "etapa": v.etapa,
                "inicio": _iso(v.inicio),
                "fin": _iso(v.fin),
                "desde": _iso(v.desde),
                "hasta": _iso(v.hasta),
            }
            for v in vs
        ],
        "metricas": metricas,
        "reacciones": [_reaccion_dict(r) for r in reacciones],
        "lineas_de_tiempo": lineas_de_tiempo,
        "imagenes": _imagenes(op, tarea),
        "script": script,
        "evaluacion": evaluar(resultado.etapas, metricas, reacciones),
        "avisos": avisos,
    }
    secretos.imprimir(
        f"{PREFIJO_RECOGIDA} {json.dumps(recogida, ensure_ascii=False, separators=(',', ':'))}"
    )
    secretos.resumen(renderizar_resumen(recogida))
    faltan = datos_faltantes(recogida)
    if faltan:
        secretos.fallar(f"{HISTORIA}: faltan datos para la conclusión: {'; '.join(faltan)}")


def test_hu_w09_detener(request: pytest.FixtureRequest) -> None:
    op = _abrir(request, "hu_w09_detener")
    vivas = tareas_hu_w09(_tareas(op, "RUNNING"))
    for tarea in vivas:
        op.aws.ecs.stop_task(
            cluster=op.contexto.cluster, task=tarea["taskArn"], reason=MOTIVO_DETENER
        )
    filas = _resumen_tareas(vivas)
    texto = (
        f"detenidas {len(filas)} (id, startedBy, estado, creada): {filas}"
        if filas
        else "ninguna tarea de HU-W09 viva: nada que detener"
    )
    op.secretos.imprimir(f"\n{HISTORIA} detener: {texto}")
    detalle = [(f[0], f"{f[1]} · {f[2]} · creada {f[3]}") for f in filas] or [
        ("Tareas", "ninguna viva")
    ]
    op.secretos.resumen(resumen_operacion("detener", [("Motivo", MOTIVO_DETENER), *detalle]))


# --- Unitarios de las funciones puras (sin AWS) ----------------------------------------------
#
# Se llaman test_hu_w09_unit_*: ningún nombre ni id de parámetro contiene hu_w09_preparar,
# hu_w09_lanzar, hu_w09_recoger ni hu_w09_detener, así que esos filtros no los seleccionan.

T0 = datetime(2026, 11, 19, 15, 0, 20, tzinfo=UTC)
T = datetime(2026, 11, 19, 15, 10, 0, tzinfo=UTC)


def _s(segundos: float) -> timedelta:
    return timedelta(seconds=segundos)


def _etapa(nombre: str = "e1", inicio_s: int = 0, duracion_s: int = 300, **extra: Any) -> dict:
    datos: dict[str, Any] = {
        "etapa": nombre,
        "tramo": "alb",
        "t0": "2026-11-19T15:00:20.000Z",
        "inicioS": inicio_s,
        "duracionS": duracion_s,
        "escala": 1,
        "pedidaPorMinuto": 500,
        "solicitudes": 2500,
        "logradaPorMinuto": 500.0,
        "errores": 0,
        "tasaError": 0,
        "errores4xx": 0,
        "errores5xx": 0,
        "erroresRed": 0,
        "p95Ms": 120.0,
        "p99Ms": 180.0,
        "dropped": 0,
    }
    datos.update(extra)
    return datos


def _linea(prefijo: str, datos: Any) -> str:
    return f"{prefijo} {json.dumps(datos)}"


# Selección y secretos


@pytest.mark.parametrize(
    ("keyword", "nombre", "esperado"),
    [
        ("", "hu_w09_preparar", False),
        (None, "hu_w09_preparar", False),
        ("hu_w09", "hu_w09_preparar", False),
        ("hu_w08", "hu_w09_lanzar", False),
        ("hu_w09_preparar", "hu_w09_preparar", True),
        ("hu_w09_preparar", "hu_w09_lanzar", False),
        ("hu_w09_preparar or hu_w09_lanzar", "hu_w09_lanzar", True),
        ("hu_w09_recoger", "hu_w09_detener", False),
    ],
    ids=[f"caso{i}" for i in range(8)],
)
def test_hu_w09_unit_seleccionada_solo_con_su_nombre(
    keyword: str | None, nombre: str, esperado: bool
) -> None:
    assert seleccionada(keyword, nombre) is esperado


def test_hu_w09_unit_redactar_oculta_los_secretos_y_las_firmas() -> None:
    emitidas: list[str] = []
    secretos = Secretos(emitidas.append, en_actions=True)
    secretos.agregar("abc", "abcdef", "", None)
    assert emitidas == ["::add-mask::abc", "::add-mask::abcdef"]
    # El más largo primero: "abcdef" no queda como "***def".
    assert secretos.redactar("x abcdef y abc z") == "x *** y *** z"
    url = (
        "https://b.s3.amazonaws.com/tmp/hu-w09/c.js?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=ASIAEJEMPLO%2F20261119&X-Amz-Date=20261119T150020Z&X-Amz-Expires=900"
        "&X-Amz-SignedHeaders=host&X-Amz-Security-Token=IQoJbEjemplo&X-Amz-Signature=deadbeef"
    )
    redactada = secretos.redactar(url)
    for valor in ("ASIAEJEMPLO", "IQoJbEjemplo", "deadbeef"):
        assert valor not in redactada
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in redactada
    assert "X-Amz-Signature=***" in redactada


def test_hu_w09_unit_mascara_solo_en_actions_una_por_linea_y_sin_repetir() -> None:
    fuera: list[str] = []
    local = Secretos(fuera.append, en_actions=False)
    local.agregar("valor")
    assert fuera == []
    assert local.redactar("el valor") == "el ***"

    emitidas: list[str] = []
    actions = Secretos(emitidas.append, en_actions=True)
    actions.agregar("uno\ndos")
    actions.agregar("uno\ndos")
    assert emitidas == ["::add-mask::uno", "::add-mask::dos"]
    assert actions.redactar("uno\ndos") == "***"


def test_hu_w09_unit_imprimir_resumen_y_fallar_redactan(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archivo = tmp_path / "resumen.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(archivo))
    secretos = Secretos(lambda _: None, en_actions=False)
    secretos.agregar("s3cr3t-valor")
    secretos.imprimir("clave s3cr3t-valor")
    assert capsys.readouterr().out == "clave ***\n"
    secretos.resumen("| s3cr3t-valor |")
    secretos.resumen("otra")
    assert archivo.read_text(encoding="utf-8") == "| *** |\n\notra\n\n"
    with pytest.raises(pytest.fail.Exception) as error:
        secretos.fallar("falló con s3cr3t-valor")
    assert "s3cr3t-valor" not in str(error.value)
    assert "***" in str(error.value)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY")
    secretos.resumen("sin archivo: no hace nada")


def test_hu_w09_unit_valores_secretos_de_las_variables_de_k6() -> None:
    variables = {
        "ALB_URL": "http://alb:8083",
        "SOCIO_CLIENT_ID": "cid",
        "SOCIO_KEY_ID": "kid",
        "APIGW_URL": "https://api",
        "APIGW_CLIENT_SECRET": "sec",
        "APIGW_API_KEY": "key",
    }
    assert sorted(valores_secretos(variables)) == ["cid", "key", "kid", "sec"]
    assert valores_secretos({"ALB_URL": "x"}) == []


# Líneas de k6


def test_hu_w09_unit_leer_resultado_k6() -> None:
    inicio = {"t0": "2026-11-19T15:00:20.000Z", "etapas": [{"etapa": "e1"}, {"etapa": "e2"}]}
    lineas = [
        "running (00m01s), 0000/0938 VUs",
        _linea("HU-W09-INICIO", inicio),
        "2026-11-19T15:30:05Z " + _linea("HU-W09-ETAPA", _etapa("e1")),
        _linea("HU-W09-ETAPA", _etapa("e2", 300)),
        _linea("HU-W09-TOKENS", {"fallidos": 0}),
        "HU-W09-ETAPAS no es una línea de etapa",
        "HU-W09 · resumen por etapa",
    ]
    resultado = leer_resultado_k6(lineas)
    assert resultado.inicio == inicio
    assert [e["etapa"] for e in resultado.etapas] == ["e1", "e2"]
    assert resultado.etapas[1]["inicioS"] == 300
    assert resultado.tokens == {"fallidos": 0}


def test_hu_w09_unit_leer_resultado_k6_sin_lineas() -> None:
    resultado = leer_resultado_k6(iter(["nada", ""]))
    assert resultado == ResultadoK6(None, [], None)


@pytest.mark.parametrize(
    "lineas",
    [
        ["HU-W09-ETAPA {no es json"],
        ["HU-W09-ETAPA [1, 2]"],
        [_linea("HU-W09-ETAPA", _etapa("e1")), _linea("HU-W09-ETAPA", _etapa("e1"))],
        [_linea("HU-W09-INICIO", {"t0": "a"}), _linea("HU-W09-INICIO", {"t0": "b"})],
        [_linea("HU-W09-ETAPA", {"etapa": "e1", "inicioS": 0})],
        [_linea("HU-W09-INICIO", {"etapas": []})],
    ],
    ids=["json", "arreglo", "etapa-repetida", "dos-inicios", "etapa-incompleta", "inicio-sin-t0"],
)
def test_hu_w09_unit_leer_resultado_k6_rechaza_lineas_invalidas(lineas: list[str]) -> None:
    with pytest.raises(ValueError):
        leer_resultado_k6(lineas)


def test_hu_w09_unit_leer_alta() -> None:
    lineas = ["init", _linea("HU-W09-ALTA", {"id": "SOC-000007", "clientId": "4abc5def6ghi"})]
    assert leer_alta(lineas) == ("SOC-000007", "4abc5def6ghi")


@pytest.mark.parametrize(
    ("lineas", "error"),
    [
        (["sin alta"], LookupError),
        ([_linea("HU-W09-ALTA", {"id": "SOC-000001", "clientId": "cid-secreto"})] * 2, ValueError),
        ([_linea("HU-W09-ALTA", {"id": "socio-1", "clientId": "cid-secreto"})], ValueError),
        ([_linea("HU-W09-ALTA", {"id": "SOC-000001", "clientId": ""})], ValueError),
    ],
    ids=["ninguna", "dos", "id-invalido", "sin-client-id"],
)
def test_hu_w09_unit_leer_alta_rechaza_y_no_muestra_el_client_id(
    lineas: list[str], error: type[Exception]
) -> None:
    with pytest.raises(error) as excepcion:
        leer_alta(lineas)
    assert "cid-secreto" not in str(excepcion.value)


def test_hu_w09_unit_instante() -> None:
    assert instante("2026-11-19T15:00:20.123Z") == datetime(2026, 11, 19, 15, 0, 20, 123000, UTC)
    assert instante(T) == T
    assert instante(T.replace(tzinfo=None)) == T
    with pytest.raises(ValueError):
        instante("ayer")


# Ventanas por etapa


def test_hu_w09_unit_ventanas_sin_el_primer_minuto_y_en_minutos_completos() -> None:
    resultado = ventanas(T0, [_etapa("e2", 300), _etapa("e1", 0)])
    assert [v.etapa for v in resultado] == ["e1", "e2"]
    e1, e2 = resultado
    assert (e1.inicio, e1.fin) == (T0, T0 + _s(300))
    assert e1.desde == datetime(2026, 11, 19, 15, 2, tzinfo=UTC)
    assert e1.hasta == datetime(2026, 11, 19, 15, 5, tzinfo=UTC)
    assert e2.desde == datetime(2026, 11, 19, 15, 7, tzinfo=UTC)
    assert e2.hasta == datetime(2026, 11, 19, 15, 10, tzinfo=UTC)


def test_hu_w09_unit_ventanas_con_t0_alineado_y_sin_descarte() -> None:
    t0 = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)
    (v,) = ventanas(t0, [_etapa("e1", 0)])
    assert (v.desde, v.hasta) == (t0 + _s(60), t0 + _s(300))
    (sin,) = ventanas(t0, [_etapa("e1", 0)], descarte_s=0)
    assert (sin.desde, sin.hasta) == (t0, t0 + _s(300))


def test_hu_w09_unit_ventanas_de_una_etapa_corta_no_se_miden() -> None:
    (v,) = ventanas(T0, [_etapa("e1", 0, 20)])
    assert (v.inicio, v.fin) == (T0, T0 + _s(20))
    assert (v.desde, v.hasta) == (None, None)
    # Bordes, con t0 alineado: 90 s dejan desde == hasta (sin minuto); 120 s, justo un minuto.
    t0 = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)
    (sin_minuto,) = ventanas(t0, [_etapa("e1", 0, 90)])
    assert (sin_minuto.desde, sin_minuto.hasta) == (None, None)
    (un_minuto,) = ventanas(t0, [_etapa("e1", 0, 120)])
    assert (un_minuto.desde, un_minuto.hasta) == (t0 + _s(60), t0 + _s(120))


# Autoescalado


def _act(segundos: float, deseadas: int | None, estado: str = "Successful") -> Actividad:
    return Actividad(T + _s(segundos), None, estado, deseadas, "")


def _tarea(nombre: str, creada_s: float | None, marcha_s: float) -> TareaNueva:
    return TareaNueva(nombre, None if creada_s is None else T + _s(creada_s), T + _s(marcha_s))


V3 = Ventana("e3", T, T + _s(300), T + _s(60), T + _s(300))
OK_ANTES = (T - _s(600), "OK")


def test_hu_w09_unit_cambios_de_alarma() -> None:
    items = [
        {
            "Timestamp": T + _s(90),
            "HistoryData": json.dumps(
                {"oldState": {"stateValue": "OK"}, "newState": {"stateValue": "ALARM"}}
            ),
            "HistorySummary": "Alarm updated from OK to ALARM",
        },
        {
            "Timestamp": T + _s(10),
            "HistoryData": "no json",
            "HistorySummary": "Alarm updated from INSUFFICIENT_DATA to OK",
        },
        {"Timestamp": T + _s(5), "HistorySummary": "Alarm updated"},
    ]
    assert cambios_de_alarma(items) == [(T + _s(10), "OK"), (T + _s(90), "ALARM")]


def test_hu_w09_unit_actividades_de() -> None:
    items = [
        {
            "StartTime": T + _s(120),
            "EndTime": T + _s(125),
            "StatusCode": "Successful",
            "Description": "Setting desired count to 3.",
            "Cause": "monitor alarm X in state ALARM",
        },
        {
            "StartTime": T + _s(10),
            "StatusCode": "Failed",
            "Description": "Setting desired count to 4.",
        },
        {"StartTime": T + _s(20), "StatusCode": "Successful", "Description": "otra cosa"},
    ]
    assert actividades_de(items) == [
        Actividad(T + _s(10), None, "Failed", 4, ""),
        Actividad(T + _s(20), None, "Successful", None, ""),
        Actividad(T + _s(120), T + _s(125), "Successful", 3, "monitor alarm X in state ALARM"),
    ]


def test_hu_w09_unit_deseadas_en() -> None:
    actividades = [_act(10, 3), _act(20, 5, "Failed"), _act(30, None), _act(40, 4)]
    assert deseadas_en(T, actividades, 2) == 2
    assert deseadas_en(T + _s(10), actividades, 2) == 2
    assert deseadas_en(T + _s(35), actividades, 2) == 3
    assert deseadas_en(T + _s(41), actividades, 2) == 4


def _evento(
    grupo: str,
    arn: str,
    estado: str,
    iniciada: str | None,
    momento: str,
    creada: str | None = "2026-11-19T15:12:05Z",
    tipo: str = "ECS Task State Change",
) -> dict[str, Any]:
    detalle: dict[str, Any] = {"group": grupo, "taskArn": arn, "lastStatus": estado}
    if iniciada:
        detalle["startedAt"] = iniciada
    if creada:
        detalle["createdAt"] = creada
    return {"detail-type": tipo, "time": momento, "detail": detalle}


def test_hu_w09_unit_tareas_en_marcha() -> None:
    base = "arn:aws:ecs:us-east-1:111:task/solventa-exp/"
    eventos = [
        _evento(
            "service:cotizacion", base + "b", "RUNNING", None, "2026-11-19T15:13:30Z", creada=None
        ),
        _evento(
            "service:cotizacion",
            base + "a",
            "RUNNING",
            "2026-11-19T15:12:50Z",
            "2026-11-19T15:13:00Z",
        ),
        _evento(
            "service:cotizacion",
            base + "a",
            "RUNNING",
            "2026-11-19T15:12:50Z",
            "2026-11-19T15:14:00Z",
        ),
        _evento("service:cotizacion", base + "c", "STOPPED", None, "2026-11-19T15:14:00Z"),
        _evento("family:solventa-exp-carga", base + "k", "RUNNING", None, "2026-11-19T15:00:00Z"),
        _evento(
            "service:clientes",
            base + "d",
            "RUNNING",
            None,
            "2026-11-19T15:15:00Z",
            tipo="ECS Service Action",
        ),
    ]
    assert tareas_en_marcha(eventos) == {
        "cotizacion": [
            TareaNueva("a", T + _s(125), T + _s(170)),
            TareaNueva("b", None, T + _s(210)),
        ]
    }


def test_hu_w09_unit_reaccion_completa() -> None:
    cambios = [OK_ANTES, (T + _s(90), "ALARM")]
    tareas = [_tarea("a", 125, 170), _tarea("vieja", -2000, -1900)]
    r = reaccion(V3, "cotizacion", cambios, [_act(120, 3)], tareas, 2, 6)
    assert (r.etapa, r.servicio) == ("e3", "cotizacion")
    assert (r.alarma, r.actividad, r.capacidad) == (T + _s(90), T + _s(120), T + _s(170))
    assert (r.deteccion_s, r.decision_s, r.capacidad_s, r.total_s) == (90, 30, 50, 170)
    assert r.nota == ""


def test_hu_w09_unit_reaccion_toma_la_ultima_tarea_nueva_de_esa_actividad() -> None:
    cambios = [OK_ANTES, (T + _s(90), "ALARM")]
    tareas = [_tarea("a", 125, 170), _tarea("b", None, 200), _tarea("siguiente", 205, 260)]
    r = reaccion(V3, "cotizacion", cambios, [_act(120, 4), _act(203, 5)], tareas, 2, 6)
    assert (r.capacidad_s, r.total_s) == (80, 200)


def test_hu_w09_unit_reaccion_al_tope() -> None:
    r = reaccion(V3, "cotizacion", [(T + _s(90), "ALARM")], [_act(-100, 6)], [], 2, 6)
    assert r.nota.startswith("al tope")
    assert r.alarma is None and r.deteccion_s is None


def test_hu_w09_unit_reaccion_sin_alarma_en_la_etapa() -> None:
    for cambios in ([OK_ANTES], [OK_ANTES, (T + _s(300), "ALARM")], []):
        r = reaccion(V3, "catalogo", cambios, [], [], 2, 6)
        assert r.alarma is None
        assert r.nota.startswith("sin ALARM")


def test_hu_w09_unit_reaccion_ya_en_alarma_al_empezar() -> None:
    cambios = [OK_ANTES, (T - _s(60), "ALARM")]
    r = reaccion(V3, "cotizacion", cambios, [_act(30, 3)], [_tarea("a", 35, 80)], 2, 6)
    assert (r.deteccion_s, r.decision_s, r.capacidad_s, r.total_s) == (0, 30, 50, 80)
    assert "ya en ALARM" in r.nota


def test_hu_w09_unit_reaccion_alarma_sin_actividad_o_sin_tarea() -> None:
    cambios = [OK_ANTES, (T + _s(90), "ALARM")]
    sin_actividad = reaccion(V3, "api-socios", cambios, [], [], 2, 6)
    assert (sin_actividad.deteccion_s, sin_actividad.decision_s) == (90, None)
    assert "sin actividad" in sin_actividad.nota
    # Una actividad hacia dentro (de 3 a 2) no es una reacción a la alarma alta.
    hacia_dentro = reaccion(V3, "api-socios", cambios, [_act(120, 2)], [], 3, 6)
    assert hacia_dentro.decision_s is None
    sin_tarea = reaccion(V3, "api-socios", cambios, [_act(120, 3)], [], 2, 6)
    assert (sin_tarea.decision_s, sin_tarea.capacidad_s) == (30, None)
    assert "sin tarea nueva" in sin_tarea.nota


def test_hu_w09_unit_reaccion_que_llega_al_maximo() -> None:
    cambios = [OK_ANTES, (T + _s(90), "ALARM")]
    r = reaccion(V3, "cotizacion", cambios, [_act(120, 6)], [_tarea("a", 125, 170)], 2, 6)
    assert r.capacidad_s == 50
    assert "llegó al máximo" in r.nota


def test_hu_w09_unit_muestras_y_linea_de_tiempo() -> None:
    serie = [(T, 2.0), (T + _s(60), 2.0), (T + _s(120), 3.0), (T + _s(180), 3.0)]
    muestras = muestras_de(serie, [_act(90, 3)], 2, T)
    assert muestras == [Muestra(0, 2, 2), Muestra(1, 3, 2), Muestra(2, 3, 3), Muestra(3, 3, 3)]
    assert linea_de_tiempo(muestras) == [
        Muestra(0, 2, 2),
        Muestra(1, 3, 2),
        Muestra(2, 3, 3),
        Muestra(3, 3, 3),
    ]
    assert linea_de_tiempo(muestras[2:]) == [Muestra(2, 3, 3), Muestra(3, 3, 3)]
    assert linea_de_tiempo([]) == []


def _politica(nombre: str, alarmas: list[str] | None, tipo: str = "TargetTrackingScaling") -> dict:
    politica: dict[str, Any] = {
        "PolicyName": nombre,
        "PolicyType": tipo,
        "TargetTrackingScalingPolicyConfiguration": {
            "TargetValue": 60.0,
            "ScaleOutCooldown": 30,
            "ScaleInCooldown": 300,
        },
    }
    if alarmas is not None:
        politica["Alarms"] = [{"AlarmName": a} for a in alarmas]
    return politica


RECURSO = "TargetTracking-service/solventa-exp/cotizacion"
ALTA = f"{RECURSO}-AlarmHigh-fc0e4183-23ac-497e-9992-691c9980c38e"
BAJA = f"{RECURSO}-AlarmLow-61a39305-ed6c-4ff8-b3f8-2b1c5e3ad1a2"


def test_hu_w09_unit_politica_de_duplicada_de_w08() -> None:
    nombre = "solventa-exp-cotizacion-cpu"
    respuesta = {"ScalingPolicies": [_politica(nombre, [BAJA, ALTA])]}
    assert politica_de(respuesta, nombre) == Politica(nombre, 60.0, 30, 300, ALTA, BAJA)
    with pytest.raises(LookupError):
        politica_de({}, nombre)
    with pytest.raises(ValueError):
        politica_de({"ScalingPolicies": [_politica(nombre, [ALTA])]}, nombre)
    with pytest.raises(ValueError):
        politica_de({"ScalingPolicies": [_politica(nombre, [ALTA, BAJA], "StepScaling")]}, nombre)


# Consultas de CloudWatch

MEDICION = Medicion(
    "solventa-exp",
    "app/solventa-exp-access/abc",
    "targetgroup/solventa-exp-api-socios/def",
    "solventa-exp",
    ("solventa-exp-cache-001", "solventa-exp-cache-002"),
)


def test_hu_w09_unit_consultas_de_etapa() -> None:
    consultas = consultas_de_etapa(MEDICION, 240)
    ids = [c.id for c in consultas]
    assert len(set(ids)) == len(ids) <= 500
    assert all(re.fullmatch(r"[a-z][A-Za-z0-9_]*", i) for i in ids)
    claves = {c.clave: c for c in consultas}
    assert len(claves) == len(consultas)

    cot = claves["cotizacion.p95"]
    assert (cot.espacio, cot.metrica, cot.estadistica, cot.periodo) == (
        "Solventa",
        "OperationDuration",
        "p95",
        240,
    )
    assert dict(cot.dimensiones) == {
        "Environment": "exp",
        "Service": "cotizacion",
        "Operation": "cotizacion",
    }
    assert claves["cotizacion.errores"].metrica == "OperationErrors"
    assert claves["cotizacion.n"].estadistica == "SampleCount"
    assert dict(claves["socio.p99"].dimensiones) == {
        "Environment": "exp",
        "Service": "api-socios",
        "Operation": "socio.cotizar",
    }

    alb = claves["alb.p95"]
    assert (alb.espacio, alb.metrica, alb.factor) == (
        "AWS/ApplicationELB",
        "TargetResponseTime",
        1000,
    )
    assert dict(alb.dimensiones) == {"LoadBalancer": MEDICION.alb, "TargetGroup": MEDICION.grupo}
    assert dict(claves["alb.5xx_elb"].dimensiones) == {"LoadBalancer": MEDICION.alb}
    assert claves["alb.solicitudes"].estadistica == "Sum"

    for servicio in SERVICIOS:
        tareas = claves[f"tareas.{servicio}"]
        assert (tareas.metrica, tareas.estadistica, tareas.periodo, tareas.agregado) == (
            "CPUUtilization",
            "SampleCount",
            60,
            "min_max",
        )
        assert dict(tareas.dimensiones) == {"ClusterName": "solventa-exp", "ServiceName": servicio}
        for clave in ("cpu", "mem"):
            assert claves[f"{clave}.{servicio}.prom"].estadistica == "Average"
            assert claves[f"{clave}.{servicio}.max"].estadistica == "Maximum"
    assert dict(claves["rds.conexiones.max"].dimensiones) == {
        "DBInstanceIdentifier": "solventa-exp"
    }
    for cache in MEDICION.cache:
        valkey = claves[f"valkey.{cache}.cpu.max"]
        assert (valkey.espacio, valkey.metrica) == ("AWS/ElastiCache", "EngineCPUUtilization")
        assert dict(valkey.dimensiones) == {"CacheClusterId": cache}


def test_hu_w09_unit_consultas_de_tareas_y_formato_aws() -> None:
    consultas = consultas_de_tareas(MEDICION)
    assert [c.clave for c in consultas] == list(SERVICIOS)
    assert {(c.estadistica, c.periodo, c.agregado) for c in consultas} == {
        ("SampleCount", 60, "serie")
    }
    (primera, *_) = a_consultas_aws(consultas)
    assert primera == {
        "Id": consultas[0].id,
        "MetricStat": {
            "Metric": {
                "Namespace": "AWS/ECS",
                "MetricName": "CPUUtilization",
                "Dimensions": [
                    {"Name": "ClusterName", "Value": "solventa-exp"},
                    {"Name": "ServiceName", "Value": "api-socios"},
                ],
            },
            "Period": 60,
            "Stat": "SampleCount",
        },
        "ReturnData": True,
    }


def test_hu_w09_unit_resumir() -> None:
    def consulta(id_: str, clave: str, stat: str, periodo: int = 240, **extra: Any) -> Consulta:
        return Consulta(id_, clave, "N", "M", (), stat, periodo, **extra)

    consultas = [
        consulta("q0", "a.p95", "p95"),
        consulta("q1", "a.n", "SampleCount"),
        consulta("q2", "tareas", "SampleCount", 60, agregado="min_max"),
        consulta("q3", "alb.p95", "p95", factor=1000),
        consulta("q4", "vacia", "Average"),
        consulta("q5", "prom", "Average"),
        consulta("q6", "serie", "SampleCount", 60, agregado="serie"),
        consulta("q7", "max", "Maximum"),
    ]
    resultados = [
        {"Id": "q0", "Timestamps": [T], "Values": [101.26]},
        {"Id": "q1", "Timestamps": [T, T + _s(60)], "Values": [10, 5]},
        {"Id": "q2", "Timestamps": [T, T + _s(60), T + _s(120)], "Values": [2, 3, 4]},
        {"Id": "q2", "Timestamps": [T + _s(180)], "Values": [5]},
        {"Id": "q3", "Timestamps": [T], "Values": [0.1234]},
        {"Id": "q4", "Timestamps": [], "Values": []},
        {"Id": "q5", "Timestamps": [T, T + _s(60)], "Values": [10, 20]},
        {"Id": "q6", "Timestamps": [T + _s(60), T], "Values": [3, 2]},
        {"Id": "q7", "Timestamps": [T, T + _s(60)], "Values": [70, 90]},
    ]
    assert resumir(consultas, resultados) == {
        "a.p95": 101.3,
        "a.n": 15,
        "tareas": {"min": 2, "max": 5},
        "alb.p95": 123.4,
        "vacia": None,
        "prom": 15.0,
        "serie": [[T.isoformat(), 2], [(T + _s(60)).isoformat(), 3]],
        "max": 90,
    }


# Candidatos, socio y variables


def test_hu_w09_unit_unico() -> None:
    assert unico(["a"], "ALB") == "a"
    with pytest.raises(LookupError, match="ningún ALB"):
        unico([], "ALB")
    with pytest.raises(ValueError, match="2 ALB: a, b"):
        unico(["a", "b"], "ALB")
    with pytest.raises(ValueError, match="A, B"):
        unico(["a", "b"], "SG", describir=str.upper)


def _clave(socio: str, creada: datetime, **cambios: Any) -> dict[str, Any]:
    clave: dict[str, Any] = {
        "id": f"id-{socio}",
        "name": f"solventa-exp-socio-{socio}",
        "description": f"Consumo atribuido al socio {socio} (Socio experimento de carga HU-W09 x)",
        "enabled": True,
        "createdDate": creada,
        "tags": {"Socio": socio, "ManagedBy": "api-socios"},
    }
    clave.update(cambios)
    return clave


def test_hu_w09_unit_socio_mas_reciente() -> None:
    claves = [
        _clave("SOC-000001", T),
        _clave("SOC-000003", T + _s(120)),
        _clave("SOC-000002", T + _s(60)),
        _clave(
            "SOC-000009", T + _s(999), description="Consumo atribuido al socio SOC-000009 (otro)"
        ),
        _clave("SOC-000008", T + _s(999), tags={"Socio": "SOC-000001"}),
        _clave("SOC-000007", T + _s(999), enabled=False),
        _clave("SOC-000006", T + _s(999), name="solventa-int-socio-SOC-000006"),
        _clave("socio-exp", T + _s(999), name="solventa-exp-socio-socio-exp", tags={}),
    ]
    assert socio_mas_reciente(claves, "solventa-exp") == (
        "SOC-000003",
        "id-SOC-000003",
        ["SOC-000001", "SOC-000002"],
    )


def test_hu_w09_unit_socio_mas_reciente_sin_candidatos_o_empatados() -> None:
    with pytest.raises(LookupError, match="hu_w09_preparar"):
        socio_mas_reciente([_clave("SOC-000001", T, enabled=False)], "solventa-exp")
    with pytest.raises(ValueError, match="SOC-000001, SOC-000002"):
        socio_mas_reciente([_clave("SOC-000001", T), _clave("SOC-000002", T)], "solventa-exp")


def test_hu_w09_unit_scope_cotizar() -> None:
    assert scope_cotizar(["solventa/consultar", "solventa/cotizar"]) == "solventa/cotizar"
    assert scope_cotizar(["solventa/consultar"]) is None
    assert scope_cotizar(["a/cotizar", "b/cotizar"]) is None


GW = TramoGw("https://api/v1", "https://t/oauth2/token", "solventa/cotizar", False, "sec", "key")


def test_hu_w09_unit_variables_de_carga_en_experimento() -> None:
    variables, nota = variables_de_carga("http://alb:8083", "cid", "kid", "experimento", GW)
    assert variables == {
        "ALB_URL": "http://alb:8083",
        "SOCIO_CLIENT_ID": "cid",
        "SOCIO_KEY_ID": "kid",
        "APIGW_URL": "https://api/v1",
        "APIGW_TOKEN_URL": "https://t/oauth2/token",
        "APIGW_CLIENT_SECRET": "sec",
        "APIGW_API_KEY": "key",
        "APIGW_SCOPE": "solventa/cotizar",
    }
    assert "experimento" in nota
    assert "sec" not in nota and "key" not in nota.replace("API key", "")


def test_hu_w09_unit_variables_de_carga_en_estandar_bajan_la_tasa() -> None:
    variables, nota = variables_de_carga("http://alb:8083", "cid", "kid", "estandar", GW)
    assert variables["APIGW_TASA"] == "2400"
    assert variables["APIGW_API_KEY"] == "key"
    assert "estandar" in nota and "3.000" in nota


@pytest.mark.parametrize(
    ("plan", "gw", "motivo"),
    [
        ("ninguno", GW, "ningún plan"),
        ("varios: estandar, experimento", GW, "ningún plan"),
        ("experimento", TramoGw("https://api/v1", "https://t", "s", True, "sec", "key"), "mTLS"),
        ("experimento", TramoGw(None, "https://t", "s", False, "sec", "key"), "api_urls.socios"),
        ("experimento", TramoGw("https://a", "https://t", None, False, "sec", "key"), "cotizar"),
        ("experimento", TramoGw("https://a", "https://t", "s", False, "", "key"), "secreto"),
    ],
    ids=["ninguno", "varios", "mtls", "sin-url", "sin-scope", "sin-secreto"],
)
def test_hu_w09_unit_variables_de_carga_sin_tramo_gw(plan: str, gw: TramoGw, motivo: str) -> None:
    variables, nota = variables_de_carga("http://alb:8083", "cid", "kid", plan, gw)
    assert variables == {
        "ALB_URL": "http://alb:8083",
        "SOCIO_CLIENT_ID": "cid",
        "SOCIO_KEY_ID": "kid",
    }
    assert nota.startswith("sin tramo gw") and motivo in nota


# Salidas de platform, cadena, suscripción y planes


def _salidas(**cambios: Any) -> dict[str, Any]:
    plataforma: dict[str, Any] = {
        "environment": "exp",
        "prefix": "solventa-exp",
        "region": "us-east-1",
        "vpc_id": "vpc-1",
        "private_subnet_ids": ["subnet-a", "subnet-b"],
        "cluster": {"name": "solventa-exp"},
        "catalog": {"api-socios": {"listener_port": 8083}, "cotizacion": {"listener_port": None}},
        "evidence_bucket": {"name": "solventa-exp-evidencias-111", "sse": "aws:kms"},
        "usage_plans": {"estandar": "p1", "experimento": "p2"},
        "events_log_group": "/ecs/solventa-exp/events",
        "alerts_topic_arn": "arn:aws:sns:us-east-1:111:solventa-exp-alertas",
        "alb": {"arn_suffix": MEDICION.alb, "security_group_id": "sg-alb"},
        "target_groups": {"api-socios": {"arn_suffix": MEDICION.grupo}},
        "database": {"identifier": "solventa-exp"},
        "cache": {"cluster_ids": list(MEDICION.cache)},
        "cognito": {"partners_pool_id": "us-east-1_Socios"},
    }
    plataforma.update(cambios)
    return {
        "platform": plataforma,
        "api_urls": {"canales": "https://c/v1", "socios": "https://s/v1"},
        "cognito": {
            "partners": {
                "token_url": "https://t/oauth2/token",
                "scopes": ["solventa/consultar", "solventa/cotizar"],
            }
        },
    }


def test_hu_w09_unit_contexto_de() -> None:
    contexto = contexto_de(_salidas())
    assert contexto == Contexto(
        region="us-east-1",
        cluster="solventa-exp",
        prefijo="solventa-exp",
        vpc="vpc-1",
        subredes=("subnet-a", "subnet-b"),
        puerto_alb=8083,
        planes={"estandar": "p1", "experimento": "p2"},
        eventos="/ecs/solventa-exp/events",
        tema_alertas="arn:aws:sns:us-east-1:111:solventa-exp-alertas",
        pool_socios="us-east-1_Socios",
        medicion=MEDICION,
        api_socios="https://s/v1",
        token_url="https://t/oauth2/token",
        scopes=("solventa/consultar", "solventa/cotizar"),
        mtls=False,
    )
    con_mtls = _salidas()
    con_mtls["api_urls"]["socios_mtls"] = "https://socios.example/v1"
    assert contexto_de(con_mtls).mtls is True
    sin_cache = contexto_de(_salidas(cache={"enabled": False, "cluster_ids": []}))
    assert sin_cache.medicion.cache == ()


@pytest.mark.parametrize(
    "cambios",
    [{"environment": "int"}, {"cluster": {"name": "solventa-int"}}, {"prefix": "solventa-int"}],
    ids=["ambiente", "cluster", "prefijo"],
)
def test_hu_w09_unit_contexto_de_rechaza_otro_ambiente(cambios: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="solventa-exp"):
        contexto_de(_salidas(**cambios))


def test_hu_w09_unit_contexto_de_nombra_lo_que_falta() -> None:
    with pytest.raises(ValueError, match="infra/platform"):
        contexto_de({})
    with pytest.raises(ValueError) as error:
        contexto_de(_salidas(events_log_group=None, catalog={}, private_subnet_ids=[]))
    for campo in ("events_log_group", "catalog", "private_subnet_ids"):
        assert campo in str(error.value)
    # El bucket de evidencias ya no hace falta: el script viaja en la definición de tarea.
    assert contexto_de(_salidas(evidence_bucket=None)).cluster == "solventa-exp"


def _servicio(
    nombre: str, deseadas: int, corriendo: int, despliegues: int = 1, estado: str = "ACTIVE"
) -> dict[str, Any]:
    return {
        "serviceName": nombre,
        "status": estado,
        "desiredCount": deseadas,
        "runningCount": corriendo,
        "deployments": [{}] * despliegues,
    }


def test_hu_w09_unit_estado_de_la_cadena() -> None:
    respuesta = {
        "services": [
            _servicio("cotizacion", 3, 2),
            _servicio("api-socios", 2, 2),
            _servicio("catalogo", 2, 2, despliegues=2),
            _servicio("clientes", 2, 2, estado="DRAINING"),
        ],
        "failures": [],
    }
    filas = estado_de_la_cadena(respuesta)
    assert [(f["servicio"], f["estable"]) for f in filas] == [
        ("api-socios", True),
        ("cotizacion", False),
        ("catalogo", False),
        ("clientes", False),
    ]
    assert filas[0]["deseadas"] == 2 and filas[0]["corriendo"] == 2
    faltante = estado_de_la_cadena({"services": [_servicio("api-socios", 2, 2)]})
    assert [f["estable"] for f in faltante] == [True, False, False, False]
    assert estado_de_la_cadena({"services": [_servicio("api-socios", 0, 0)]})[0]["estable"] is False


def test_hu_w09_unit_estado_suscripciones_sin_los_correos() -> None:
    suscripciones = [
        {"SubscriptionArn": "PendingConfirmation", "Protocol": "email", "Endpoint": "a@b.co"},
        {
            "SubscriptionArn": "arn:aws:sns:us-east-1:111:t:abc",
            "Protocol": "email",
            "Endpoint": "c@d.co",
        },
    ]
    estado = estado_suscripciones(suscripciones)
    assert estado == {
        "confirmadas": 1,
        "pendientes": 1,
        "protocolos": ["email"],
        "estado": "1 confirmada, 1 PendingConfirmation",
    }
    assert "@" not in json.dumps(estado)
    assert estado_suscripciones([])["estado"] == "sin suscripciones"


def test_hu_w09_unit_plan_efectivo() -> None:
    planes = {"estandar": ["k1", "k2"], "experimento": ["k3"], "basico": []}
    assert plan_efectivo("k3", planes) == "experimento"
    assert plan_efectivo("k1", planes) == "estandar"
    assert plan_efectivo("k9", planes) == "ninguno"
    assert plan_efectivo("k1", {"estandar": ["k1"], "experimento": ["k1"]}) == (
        "varios: estandar, experimento"
    )


# Tareas de ECS


def _tarea_ecs(startedby: str, creada: datetime, **extra: Any) -> dict[str, Any]:
    tarea = {
        "taskArn": f"arn:aws:ecs:us-east-1:111:task/solventa-exp/{startedby}-{creada:%H%M%S}",
        "startedBy": startedby,
        "createdAt": creada,
    }
    tarea.update(extra)
    return tarea


def test_hu_w09_unit_tareas_hu_w09_y_ultima() -> None:
    tareas = [
        _tarea_ecs("hu-w09", T),
        _tarea_ecs("hu-w09-alta", T + _s(60)),
        _tarea_ecs("ecs-svc/123", T + _s(120)),
        _tarea_ecs("hu-w09", T + _s(30)),
        {"taskArn": "sin-started-by", "createdAt": T},
    ]
    assert [t["startedBy"] for t in tareas_hu_w09(tareas)] == ["hu-w09", "hu-w09-alta", "hu-w09"]
    exactas = tareas_hu_w09(tareas, exacto="hu-w09")
    assert len(exactas) == 2
    assert ultima(exactas) is tareas[3]
    assert ultima([]) is None


def test_hu_w09_unit_salida_de() -> None:
    tarea = {
        "lastStatus": "STOPPED",
        "stopCode": "EssentialContainerExited",
        "stoppedReason": "Essential container in task exited",
        "containers": [
            {"name": "otro", "exitCode": 1},
            {"name": "k6", "exitCode": 0, "reason": None},
        ],
        "overrides": {
            "containerOverrides": [{"environment": [{"name": "APIGW_API_KEY", "value": "x"}]}]
        },
    }
    assert salida_de(tarea) == {
        "estado": "STOPPED",
        "exitCode": 0,
        "stopCode": "EssentialContainerExited",
        "stoppedReason": "Essential container in task exited",
        "motivoContenedor": None,
    }
    assert salida_de({})["exitCode"] is None


def test_hu_w09_unit_imagenes_de() -> None:
    definicion = {
        "containerDefinitions": [
            {
                "name": "cotizacion",
                "image": "111.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/cotizacion:abc",
            },
            {"name": "k6", "image": "grafana/k6:1.8.2@sha256:cbdf"},
        ]
    }
    tarea = {"containers": [{"name": "cotizacion", "imageDigest": "sha256:1234"}]}
    assert imagenes_de(definicion, tarea) == [
        {
            "contenedor": "cotizacion",
            "imagen": "111.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/cotizacion:abc",
            "digest": "sha256:1234",
        },
        {"contenedor": "k6", "imagen": "grafana/k6:1.8.2@sha256:cbdf", "digest": "sha256:cbdf"},
    ]
    assert imagenes_de(definicion, None)[0]["digest"] is None


def test_hu_w09_unit_flujo_k6() -> None:
    assert flujo_k6("arn:aws:ecs:us-east-1:111:task/solventa-exp/abc123") == "k6/k6/abc123"


def test_hu_w09_unit_sobrescrituras_solo_variables() -> None:
    assert sobrescrituras({"SOCIO_KEY_ID": "kid", "ALB_URL": "http://alb:8083"}) == {
        "containerOverrides": [
            {
                "name": "k6",
                "environment": [
                    {"name": "ALB_URL", "value": "http://alb:8083"},
                    {"name": "SOCIO_KEY_ID", "value": "kid"},
                ],
            }
        ]
    }
    # Las de una corrida real (longitudes típicas de Cognito, API Gateway y el ALB) caben de sobra.
    reales = {
        "ALB_URL": "http://internal-solventa-exp-access-1234567890.us-east-1.elb.amazonaws.com:8083",
        "SOCIO_CLIENT_ID": "c" * 26,
        "SOCIO_KEY_ID": "k" * 10,
        "APIGW_URL": "https://abcdefghij.execute-api.us-east-1.amazonaws.com/v1",
        "APIGW_TOKEN_URL": "https://solventa-exp-socios-111.auth.us-east-1.amazoncognito.com/oauth2/token",
        "APIGW_CLIENT_SECRET": "s" * 52,
        "APIGW_API_KEY": "a" * 40,
        "APIGW_SCOPE": "solventa/cotizar",
        "APIGW_TASA": "2400",
    }
    assert len(json.dumps(sobrescrituras(reales))) < 1000


def test_hu_w09_unit_sobrescrituras_en_el_limite_de_8192_sin_mostrar_valores() -> None:
    # RunTask admite 8.192 caracteres de overrides, contando el JSON (se mide con json.dumps).
    vacia = {"containerOverrides": [{"name": "k6", "environment": [{"name": "V", "value": ""}]}]}
    base = len(json.dumps(vacia))
    assert len(json.dumps(sobrescrituras({"V": "x" * (8191 - base)}))) == 8191
    with pytest.raises(ValueError, match="8192") as error:
        sobrescrituras({"V": "x" * (8192 - base)})
    assert "xxxx" not in str(error.value)


def _definicion_k6(
    variables: list[dict[str, str]] | None, revision: str = "solventa-exp-carga:7"
) -> dict:
    contenedor: dict[str, Any] = {"name": "k6", "image": "grafana/k6:1.8.2@sha256:cbdf"}
    if variables is not None:
        contenedor["environment"] = variables
    return {
        "taskDefinitionArn": f"arn:aws:ecs:us-east-1:111:task-definition/{revision}",
        "containerDefinitions": [{"name": "otro", "image": "x"}, contenedor],
    }


def test_hu_w09_unit_huella_del_script() -> None:
    script = b"export default function () {}\n"
    huella = hashlib.sha256(script).hexdigest()
    definicion = _definicion_k6(
        [
            {"name": "ALB_URL", "value": "http://alb"},
            {"name": "HU_W09_SCRIPT", "value": base64.b64encode(script).decode()},
        ]
    )
    tarea = {"containers": [{"name": "k6", "imageDigest": "sha256:abcd"}]}
    assert huella_del_script(definicion, tarea, script) == {
        "revision": "solventa-exp-carga:7",
        "sha256": huella,
        "sha256Commit": huella,
        "coincide": True,
        "imagen": "grafana/k6:1.8.2@sha256:cbdf",
        "digest": "sha256:abcd",
        "nota": "",
    }
    distinto = huella_del_script(definicion, None, b"otro")
    assert (distinto["coincide"], distinto["digest"]) == (False, "sha256:cbdf")
    assert "distinto" in distinto["nota"]
    sin_commit = huella_del_script(definicion, tarea, None)
    assert (sin_commit["sha256Commit"], sin_commit["coincide"]) == (None, None)


@pytest.mark.parametrize(
    ("variables", "nota"),
    [
        (None, "HU_W09_SCRIPT"),
        ([{"name": "ALB_URL", "value": "x"}], "HU_W09_SCRIPT"),
        ([{"name": "HU_W09_SCRIPT", "value": "aG9sYQ==%%%"}], "base64"),
    ],
    ids=["sin-variables", "sin-script", "base64-invalido"],
)
def test_hu_w09_unit_huella_del_script_sin_script_legible(variables: Any, nota: str) -> None:
    huella = huella_del_script(_definicion_k6(variables), None, b"x")
    assert (huella["sha256"], huella["coincide"]) == (None, False)
    assert nota in huella["nota"]


CADENA_CPU = [
    {"servicio": "api-socios", "cpu": "256", "deseadas": 2, "maximo": 6},
    {"servicio": "cotizacion", "cpu": "512", "deseadas": 2, "maximo": 6},
    {"servicio": "catalogo", "cpu": "256", "deseadas": 2, "maximo": 6},
    {"servicio": "clientes", "cpu": "256", "deseadas": 3, "maximo": 6},
]


def test_hu_w09_unit_necesidad_vcpu() -> None:
    assert necesidad_vcpu(CADENA_CPU, "2048") == {
        "actual": 4.75,
        "maxima": 9.5,
        "k6": 2.0,
        "faltan": [],
    }
    incompleta = necesidad_vcpu(
        [*CADENA_CPU[:3], {"servicio": "clientes", "cpu": None, "deseadas": 2, "maximo": None}],
        None,
    )
    assert incompleta == {"actual": 2.0, "maxima": 6.0, "k6": None, "faltan": ["clientes", "k6"]}


def test_hu_w09_unit_filas_de_cuotas() -> None:
    necesidad = {"actual": 4.75, "maxima": 9.5, "k6": 2.0, "faltan": []}
    cuotas = {
        "L-3032A538": {
            "nombre": "Fargate On-Demand vCPU resource count",
            "valor": 6.0,
            "origen": "aplicada",
        },
        "L-36FBB829": {
            "nombre": "Fargate Spot vCPU resource count",
            "valor": None,
            "origen": "no se pudo leer (AccessDeniedException)",
        },
    }
    filas = dict(filas_de_cuotas(cuotas, necesidad))
    assert filas["Cuota Fargate On-Demand vCPU resource count (L-3032A538)"] == "6 vCPU (aplicada)"
    assert filas["Cuota Fargate Spot vCPU resource count (L-36FBB829)"] == (
        "no se pudo leer (AccessDeniedException)"
    )
    assert (
        filas["vCPU estimadas (cadena y k6)"] == "4.75 ahora · 9.5 con la cadena al máximo (k6: 2)"
    )
    assert filas["Holgura On-Demand"].startswith("no alcanza")
    mas = {**cuotas, "L-3032A538": {**cuotas["L-3032A538"], "valor": 12.0}}
    assert dict(filas_de_cuotas(mas, necesidad))["Holgura On-Demand"].startswith("alcanza")
    sin_leer = dict(filas_de_cuotas({}, {**necesidad, "faltan": ["clientes"]}))
    assert sin_leer["Holgura On-Demand"].startswith("sin la cuota On-Demand")
    assert "faltan: clientes" in sin_leer["vCPU estimadas (cadena y k6)"]


# Evaluación preliminar y resumen


@pytest.mark.parametrize(
    ("dropped", "p99", "esperado"),
    [
        (0, 5000.0, "válida"),
        (5, 1600.0, "cuenta: cuello del sistema"),
        (5, 1599.9, "fuera: faltó k6"),
        (5, None, "fuera: faltó k6"),
    ],
    ids=["sin-dropped", "cuello", "falto-k6", "sin-p99"],
)
def test_hu_w09_unit_validez(dropped: int, p99: float | None, esperado: str) -> None:
    assert validez(_etapa(dropped=dropped, p99Ms=p99)) == esperado


def test_hu_w09_unit_criterio_c4() -> None:
    def r(servicio: str, **campos: Any) -> Reaccion:
        return Reaccion("e3", servicio, **campos)

    assert criterio_c4([r("cotizacion", nota="sin ALARM en la etapa")]) == "sin escalado requerido"
    assert criterio_c4([r("cotizacion", nota="al tope: 6 de 6")]) == "al tope: cotizacion"
    assert (
        criterio_c4(
            [r("cotizacion", alarma=T, actividad=T + _s(30)), r("clientes", nota="al tope: 6 de 6")]
        )
        == "cumple: escaló cotizacion; al tope: clientes"
    )
    assert (
        criterio_c4([r("cotizacion", alarma=T, actividad=T + _s(30)), r("api-socios", alarma=T)])
        == "no cumple: api-socios en ALARM sin escalar"
    )
    assert criterio_c4([]) == "sin datos"


def test_hu_w09_unit_evaluar_c2_y_c3_en_el_limite() -> None:
    etapas = [
        _etapa("e1", logradaPorMinuto=475.0, tasaError=0.01),
        _etapa("e2", 300, logradaPorMinuto=474.9, tasaError=0.0101),
        _etapa("e3", 600, logradaPorMinuto=500.0, tasaError=0.0, dropped=3, p99Ms=100.0),
    ]
    metricas = {
        "e1": {"cotizacion.n": 1000, "cotizacion.errores": 10},
        "e2": {"cotizacion.n": 1000, "cotizacion.errores": 0},
        "e3": {"cotizacion.n": 500, "cotizacion.errores": 6},
    }
    reacciones = [Reaccion("e1", "cotizacion", nota="sin ALARM en la etapa")]
    filas = evaluar(etapas, metricas, reacciones)
    assert [(f["etapa"], f["c2"], f["c3"]) for f in filas] == [
        ("e1", True, True),
        ("e2", False, False),
        ("e3", True, False),
    ]
    assert [f["cuenta"] for f in filas] == [True, True, False]
    assert filas[0]["tasaLograda"] == 0.95
    assert filas[2]["errorCotizacion"] == 0.012
    assert filas[0]["c4"] == "sin escalado requerido"
    assert filas[1]["c4"] == "sin datos"
    sin_metricas = evaluar([_etapa("e1", tasaError=None, pedidaPorMinuto=0)], {}, [])
    assert (sin_metricas[0]["c2"], sin_metricas[0]["c3"], sin_metricas[0]["errorCotizacion"]) == (
        None,
        None,
        None,
    )


def _recogida() -> dict[str, Any]:
    etapas = [_etapa("e1"), _etapa("e2", 300)]
    return {
        "tarea": {"id": "abc123", "estado": "STOPPED", "exitCode": 0, "stoppedReason": "fin"},
        "inicio": {
            "t0": "2026-11-19T15:00:20.000Z",
            "tramos": ["alb"],
            "etapas": [{"etapa": "e1"}, {"etapa": "e2"}],
        },
        "tokens": None,
        "etapas": etapas,
        "ventanas": [
            {
                "etapa": "e1",
                "desde": "2026-11-19T15:02:00+00:00",
                "hasta": "2026-11-19T15:05:00+00:00",
            }
        ],
        "metricas": {
            "e1": {"cotizacion.p95": 101.3, "tareas.cotizacion": {"min": 2, "max": 3}},
            "e2": {"cotizacion.p95": None},
        },
        "reacciones": [{"etapa": "e1", "servicio": "cotizacion", "nota": "sin ALARM en la etapa"}],
        "lineas_de_tiempo": {"cotizacion": [[0, 2, 2], [6.0, 3, 2]]},
        "imagenes": {
            "cotizacion": [{"contenedor": "cotizacion", "imagen": "repo:tag", "digest": "sha256:1"}]
        },
        "evaluacion": evaluar(etapas, {}, []),
        "script": {
            "revision": "solventa-exp-carga:7",
            "sha256": "ab12",
            "sha256Commit": "ab12",
            "coincide": True,
            "imagen": "grafana/k6:1.8.2@sha256:cbdf",
            "digest": "sha256:cbdf",
            "nota": "",
        },
        "avisos": ["un aviso | con barra"],
    }


def test_hu_w09_unit_renderizar_resumen() -> None:
    texto = renderizar_resumen(_recogida())
    for titulo in (
        "k6 por etapa",
        "CloudWatch por etapa",
        "Tareas por servicio",
        "Recursos",
        "Reacción del autoescalado",
        "Línea de tiempo",
        "Imágenes",
        "Script de k6",
        "Evaluación preliminar",
        "Avisos",
    ):
        assert titulo in texto, titulo
    assert "| e1 | alb | 500 |" in texto
    assert "101.3" in texto and "2–3" in texto
    assert "un aviso \\| con barra" in texto
    assert "`abc123`" in texto
    assert "| solventa-exp-carga:7 | ab12 | ab12 | sí |" in texto
    assert len(texto.encode()) < LIMITE_RESUMEN


@pytest.mark.parametrize(
    ("tokens", "esperado"),
    [
        ({"fallidos": 5, "sin_token": 21}, "tokens fallidos: 5 · iteraciones sin token: 21"),
        ({"fallidos": 0, "sin_token": 0}, "tokens fallidos: 0 · iteraciones sin token: 0"),
        # Una línea HU-W09-TOKENS de antes de sin_token: no falla.
        ({"fallidos": 2}, "tokens fallidos: 2 · iteraciones sin token: sin dato"),
        # Sin tramo por API Gateway no hay línea HU-W09-TOKENS.
        (None, "tokens fallidos: — · iteraciones sin token: —"),
    ],
)
def test_hu_w09_unit_resumen_iteraciones_sin_token(
    tokens: dict[str, int] | None, esperado: str
) -> None:
    recogida = _recogida()
    recogida["tokens"] = tokens
    assert esperado in renderizar_resumen(recogida)


def test_hu_w09_unit_limitar() -> None:
    assert limitar("corto", 100) == "corto"
    largo = "línea\n" * 1000
    recortado = limitar(largo, 200)
    assert len(recortado.encode()) <= 200
    assert recortado.endswith("(recortado: el JSON completo está en el log del job)")


def test_hu_w09_unit_resumen_operacion() -> None:
    cadena = estado_de_la_cadena({"services": [_servicio("api-socios", 2, 2)]})
    texto = resumen_operacion("preparar", [("Socio", "SOC-000001"), ("Plan", None)], cadena)
    assert texto.startswith("### HU-W09 · preparar")
    assert "| Socio | SOC-000001 |" in texto
    assert "| Plan | — |" in texto
    assert "#### Cadena" in texto
    assert "| api-socios | ACTIVE | 2 | 2 | 1 | sí |" in texto
    assert "| cotizacion | no existe | — | — | 0 | no |" in texto
    assert "#### Cadena" not in resumen_operacion("detener", [("Motivo", "m")])


def test_hu_w09_unit_datos_faltantes() -> None:
    assert datos_faltantes(_recogida()) == []
    sin_nada = datos_faltantes({"inicio": None, "etapas": [], "metricas": {}})
    assert any("HU-W09-INICIO" in f for f in sin_nada)
    assert any("HU-W09-ETAPA" in f for f in sin_nada)
    incompleta = _recogida()
    incompleta["etapas"] = incompleta["etapas"][:1]
    assert datos_faltantes(incompleta) == ["las líneas HU-W09-ETAPA de e2"]
    sin_cotizacion = _recogida()
    sin_cotizacion["metricas"] = {"e1": {"cotizacion.p95": None}}
    assert datos_faltantes(sin_cotizacion) == [
        "OperationDuration de cotizacion en todas las etapas"
    ]
