"""HU-W08: capacidad de cotizacion para Operación › Tablero (GET /operacion/capacidad).

Clientes boto3 reales con `botocore.stub.Stubber`: cada solicitud se compara con la esperada
(dimensiones, período, ResourceId) y nada sale a la red.
"""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from bff_web.adapters.inbound import operacion as ruta
from bff_web.adapters.outbound.capacidad import RepositorioCapacidad
from bff_web.application.consultar_capacidad import ConsultarCapacidad
from bff_web.config import Settings, settings
from bff_web.main import app
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from solventa_common import telemetry

AHORA = datetime(2026, 11, 18, 15, 0, 30, tzinfo=UTC)
FIN = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)  # último minuto completo
DESDE = FIN - timedelta(minutes=60)
CLUSTER = "solventa-int"
SERVICIO = "cotizacion"
NAMESPACE = "Solventa"
RECURSO = f"service/{CLUSTER}/{SERVICIO}"
DIMENSION = "ecs:service:DesiredCount"
OPERACION = {"X-Authenticated-Groups": "[operacion]", "X-Correlation-Id": "w08-capacidad-1"}

CONSULTAS = [
    {
        "Id": "cotizaciones",
        "MetricStat": {
            "Metric": {
                "Namespace": NAMESPACE,
                "MetricName": "OperationDuration",
                "Dimensions": [
                    {"Name": "Environment", "Value": "int"},
                    {"Name": "Service", "Value": SERVICIO},
                    {"Name": "Operation", "Value": "cotizacion"},
                ],
            },
            "Period": 60,
            "Stat": "SampleCount",
        },
    },
    {
        "Id": "tareas",
        "MetricStat": {
            "Metric": {
                "Namespace": "AWS/ECS",
                "MetricName": "CPUUtilization",
                "Dimensions": [
                    {"Name": "ClusterName", "Value": CLUSTER},
                    {"Name": "ServiceName", "Value": SERVICIO},
                ],
            },
            "Period": 60,
            "Stat": "SampleCount",
        },
    },
]


def minuto(n: int) -> datetime:
    """El minuto n de la ventana (0 = DESDE)."""
    return DESDE + timedelta(minutes=n)


def resultado(identificador: str, puntos: dict[datetime, float]) -> dict[str, Any]:
    return {
        "Id": identificador,
        "Label": identificador,
        "Timestamps": list(puntos),
        "Values": list(puntos.values()),
        "StatusCode": "Complete",
    }


def esperar_metricas(
    cw: Stubber,
    cotizaciones: dict[datetime, float],
    tareas: dict[datetime, float],
    token: str | None = None,
    siguiente: str | None = None,
) -> None:
    parametros: dict[str, Any] = {
        "MetricDataQueries": CONSULTAS,
        "StartTime": DESDE,
        "EndTime": FIN,
        "ScanBy": "TimestampAscending",
    }
    if token:
        parametros["NextToken"] = token
    respuesta: dict[str, Any] = {
        "MetricDataResults": [resultado("cotizaciones", cotizaciones), resultado("tareas", tareas)]
    }
    if siguiente:
        respuesta["NextToken"] = siguiente
    cw.add_response("get_metric_data", respuesta, parametros)


def esperar_limites(aas: Stubber, minimo: int = 2, maximo: int = 4, hay: bool = True) -> None:
    objetivo = {
        "ServiceNamespace": "ecs",
        "ResourceId": RECURSO,
        "ScalableDimension": DIMENSION,
        "MinCapacity": minimo,
        "MaxCapacity": maximo,
        "RoleARN": "arn:aws:iam::000000000000:role/ecs-autoscaling",
        "CreationTime": DESDE,
    }
    aas.add_response(
        "describe_scalable_targets",
        {"ScalableTargets": [objetivo] if hay else []},
        {"ServiceNamespace": "ecs", "ResourceIds": [RECURSO], "ScalableDimension": DIMENSION},
    )


def actividad(
    inicio: datetime,
    fin: datetime | None = None,
    estado: str = "Successful",
    descripcion: str = "Setting desired count to 3.",
) -> dict[str, Any]:
    datos: dict[str, Any] = {
        "ActivityId": f"act-{inicio:%H%M%S}",
        "ServiceNamespace": "ecs",
        "ResourceId": RECURSO,
        "ScalableDimension": DIMENSION,
        "Description": descripcion,
        "Cause": "monitor alarm TargetTracking-service/x-AlarmHigh in state ALARM",
        "StartTime": inicio,
        "StatusCode": estado,
    }
    if fin is not None:
        datos["EndTime"] = fin
    return datos


def esperar_actividades(
    aas: Stubber,
    actividades: list[dict[str, Any]],
    token: str | None = None,
    siguiente: str | None = None,
) -> None:
    parametros: dict[str, Any] = {
        "ServiceNamespace": "ecs",
        "ResourceId": RECURSO,
        "ScalableDimension": DIMENSION,
    }
    if token:
        parametros["NextToken"] = token
    respuesta: dict[str, Any] = {"ScalingActivities": actividades}
    if siguiente:
        respuesta["NextToken"] = siguiente
    aas.add_response("describe_scaling_activities", respuesta, parametros)


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, Any, Stubber, Stubber]]:
    # Credenciales falsas: el Stubber responde antes de firmar y nada sale a la red.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "prueba")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "prueba")
    cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")
    autoscaling = boto3.client("application-autoscaling", region_name="us-east-1")
    with Stubber(cloudwatch) as cw, Stubber(autoscaling) as aas:
        yield cloudwatch, autoscaling, cw, aas


def caso(cloudwatch: Any, autoscaling: Any) -> ConsultarCapacidad:
    repositorio = RepositorioCapacidad(cloudwatch, autoscaling, NAMESPACE, "int", CLUSTER, SERVICIO)
    return ConsultarCapacidad(repositorio, ahora=lambda: AHORA)


# --- Adaptador y caso de uso ---------------------------------------------------------------
def test_solicitudes_exactas_a_cloudwatch_y_auto_scaling(aws: Any) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {minuto(0): 4.0}, {minuto(59): 2.0})
    esperar_limites(aas)
    esperar_actividades(aas, [])
    panel = caso(cloudwatch, autoscaling).ejecutar()
    cw.assert_no_pending_responses()
    aas.assert_no_pending_responses()
    assert (panel.servicio, panel.ventana_min, panel.minimo, panel.maximo) == (SERVICIO, 60, 2, 4)


def test_la_serie_rellena_con_cero_los_minutos_sin_dato(aws: Any) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {minuto(0): 3.0, minuto(30): 7.0, minuto(59): 2.0}, {})
    esperar_limites(aas)
    esperar_actividades(aas, [])
    serie = caso(cloudwatch, autoscaling).ejecutar().cotizaciones_por_minuto
    assert [p.instante for p in serie] == [minuto(n) for n in range(60)]
    assert {n: p.valor for n, p in enumerate(serie) if p.valor} == {0: 3, 30: 7, 59: 2}
    assert sum(1 for p in serie if p.valor == 0) == 57


def test_sin_datos(aws: Any) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {}, {})
    esperar_limites(aas, 2, 4)
    esperar_actividades(aas, [])
    panel = caso(cloudwatch, autoscaling).ejecutar()
    assert [p.valor for p in panel.cotizaciones_por_minuto] == [0] * 60
    # Sin puntos de CPUUtilization no se sabe cuántas tareas hay: null, no 0.
    assert (panel.tareas_activas, panel.ultimo_escalado) == (None, None)


def test_tareas_activas_es_el_ultimo_punto(aws: Any) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {}, {minuto(57): 2.0, minuto(59): 3.0, minuto(58): 4.0})
    esperar_limites(aas)
    esperar_actividades(aas, [])
    assert caso(cloudwatch, autoscaling).ejecutar().tareas_activas == 3


def test_get_metric_data_paginado(aws: Any) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {minuto(1): 5.0}, {minuto(1): 2.0}, siguiente="p2")
    esperar_metricas(cw, {minuto(2): 6.0}, {minuto(2): 3.0}, token="p2")
    esperar_limites(aas)
    esperar_actividades(aas, [])
    panel = caso(cloudwatch, autoscaling).ejecutar()
    cw.assert_no_pending_responses()
    assert [p.valor for p in panel.cotizaciones_por_minuto[:4]] == [0, 5, 6, 0]
    assert panel.tareas_activas == 3


def test_ultimo_escalado_es_la_actividad_mas_reciente_dentro_de_la_ventana(aws: Any) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {}, {})
    esperar_limites(aas)
    reciente = minuto(40)
    esperar_actividades(
        aas,
        [
            actividad(minuto(10), minuto(11)),
            actividad(reciente, reciente + timedelta(seconds=70)),
            actividad(DESDE - timedelta(minutes=10), DESDE - timedelta(minutes=9)),
        ],
    )
    escalado = caso(cloudwatch, autoscaling).ejecutar().ultimo_escalado
    assert escalado is not None
    assert (escalado.inicio, escalado.fin, escalado.duracion_s, escalado.estado) == (
        reciente,
        reciente + timedelta(seconds=70),
        70,
        "Successful",
    )
    assert escalado.descripcion == "Setting desired count to 3."


def test_actividad_fuera_de_la_ventana_no_cuenta(aws: Any) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {}, {})
    esperar_limites(aas)
    justo_antes = DESDE - timedelta(seconds=1)
    esperar_actividades(aas, [actividad(justo_antes, justo_antes + timedelta(seconds=40))])
    assert caso(cloudwatch, autoscaling).ejecutar().ultimo_escalado is None


def test_actividad_en_el_borde_de_la_ventana_cuenta(aws: Any) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {}, {})
    esperar_limites(aas)
    esperar_actividades(aas, [actividad(DESDE, DESDE + timedelta(seconds=30))])
    escalado = caso(cloudwatch, autoscaling).ejecutar().ultimo_escalado
    assert escalado is not None and escalado.inicio == DESDE


def test_actividad_en_curso(aws: Any) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {}, {})
    esperar_limites(aas)
    esperar_actividades(aas, [actividad(minuto(58), estado="InProgress")])
    escalado = caso(cloudwatch, autoscaling).ejecutar().ultimo_escalado
    assert escalado is not None
    assert (escalado.inicio, escalado.fin, escalado.duracion_s, escalado.estado) == (
        minuto(58),
        None,
        None,
        "InProgress",
    )


def test_actividades_paginadas_hasta_salir_de_la_ventana(aws: Any) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {}, {})
    esperar_limites(aas)
    # AWS lista de la más reciente a la más antigua: la segunda página ya sale de la ventana
    # y no se pide la tercera.
    esperar_actividades(aas, [actividad(minuto(20), minuto(21))], siguiente="p2")
    esperar_actividades(
        aas,
        [actividad(minuto(50), minuto(51)), actividad(minuto(0) - timedelta(hours=2))],
        token="p2",
        siguiente="p3",
    )
    escalado = caso(cloudwatch, autoscaling).ejecutar().ultimo_escalado
    aas.assert_no_pending_responses()
    assert escalado is not None and escalado.inicio == minuto(50)


def test_sin_objetivo_de_escalado_falla(aws: Any) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {}, {})
    esperar_limites(aas, hay=False)
    with pytest.raises(LookupError):
        caso(cloudwatch, autoscaling).ejecutar()


def test_mide_la_operacion_consultar_capacidad(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cloudwatch, autoscaling, cw, aas = aws
    esperar_metricas(cw, {}, {})
    esperar_limites(aas)
    esperar_actividades(aas, [])
    medidas: list[tuple[str, str]] = []
    monkeypatch.setattr(
        telemetry,
        "registrar",
        lambda nombre, _ms, resultado, **_: medidas.append((nombre, resultado)),
    )
    caso(cloudwatch, autoscaling).ejecutar()
    assert medidas == [("consultar_capacidad", "ok")]


# --- Configuración y fábrica ---------------------------------------------------------------
def test_configuracion_desde_terraform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPACIDAD_CLUSTER", CLUSTER)
    monkeypatch.setenv("CAPACIDAD_SERVICIO", SERVICIO)
    monkeypatch.setenv("METRICS_NAMESPACE", "Solventa/QA")
    leida = Settings()
    assert (leida.capacidad_cluster, leida.capacidad_servicio, leida.metrics_namespace) == (
        CLUSTER,
        SERVICIO,
        "Solventa/QA",
    )


def test_sin_configuracion_de_capacidad(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("CAPACIDAD_CLUSTER", "CAPACIDAD_SERVICIO", "METRICS_NAMESPACE"):
        monkeypatch.delenv(variable, raising=False)
        monkeypatch.delenv(f"SOLVENTA_{variable}", raising=False)
    leida = Settings()
    # El namespace por omisión es el de infra/apps (var.metrics_namespace).
    assert (leida.capacidad_cluster, leida.capacidad_servicio, leida.metrics_namespace) == (
        None,
        None,
        "Solventa",
    )


@pytest.fixture
def configurada(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "prueba")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "prueba")
    monkeypatch.setattr(settings, "capacidad_cluster", CLUSTER)
    monkeypatch.setattr(settings, "capacidad_servicio", SERVICIO)
    ruta.consultar_capacidad.cache_clear()
    yield
    ruta.consultar_capacidad.cache_clear()


def test_construye_el_caso_con_la_configuracion(configurada: None) -> None:
    construido = ruta.consultar_capacidad()
    assert construido is not None
    repositorio = construido.repositorio
    assert isinstance(repositorio, RepositorioCapacidad)
    assert (
        repositorio.cloudwatch.meta.service_model.service_name,
        repositorio.autoscaling.meta.service_model.service_name,
    ) == ("cloudwatch", "application-autoscaling")
    assert (
        repositorio.namespace,
        repositorio.ambiente,
        repositorio.cluster,
        repositorio.servicio,
    ) == (settings.metrics_namespace, settings.environment, CLUSTER, SERVICIO)


@pytest.mark.parametrize("faltante", ["capacidad_cluster", "capacidad_servicio"])
def test_sin_configuracion_no_hay_caso(
    configurada: None, monkeypatch: pytest.MonkeyPatch, faltante: str
) -> None:
    monkeypatch.setattr(settings, faltante, None)
    ruta.consultar_capacidad.cache_clear()
    assert ruta.consultar_capacidad() is None


# --- Ruta ----------------------------------------------------------------------------------
@pytest.fixture
def http(aws: Any) -> Iterator[tuple[TestClient, Stubber, Stubber]]:
    cloudwatch, autoscaling, cw, aas = aws
    app.dependency_overrides[ruta.consultar_capacidad] = lambda: caso(cloudwatch, autoscaling)
    yield TestClient(app), cw, aas
    app.dependency_overrides.clear()


def test_panel_de_capacidad(http: Any) -> None:
    cliente, cw, aas = http
    esperar_metricas(cw, {minuto(0): 12.0, minuto(41): 30.0}, {minuto(59): 3.0})
    esperar_limites(aas, 2, 4)
    esperar_actividades(aas, [actividad(minuto(40), minuto(40) + timedelta(seconds=41))])
    respuesta = cliente.get("/operacion/capacidad", headers=OPERACION)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    serie = cuerpo.pop("cotizaciones_por_minuto")
    assert cuerpo == {
        "ambiente": settings.environment,
        "servicio": SERVICIO,
        "ventana_min": 60,
        "tareas_activas": 3,
        "minimo": 2,
        "maximo": 4,
        "ultimo_escalado": {
            "inicio": "2026-11-18T14:40:00Z",
            "fin": "2026-11-18T14:40:41Z",
            "duracion_s": 41,
            "descripcion": "Setting desired count to 3.",
            "estado": "Successful",
        },
    }
    assert len(serie) == 60
    assert serie[0] == {"instante": "2026-11-18T14:00:00Z", "valor": 12}
    assert serie[41] == {"instante": "2026-11-18T14:41:00Z", "valor": 30}
    assert serie[-1] == {"instante": "2026-11-18T14:59:00Z", "valor": 0}


def test_sin_escalado_en_la_ventana_es_null(http: Any) -> None:
    cliente, cw, aas = http
    esperar_metricas(cw, {}, {})
    esperar_limites(aas)
    esperar_actividades(aas, [])
    respuesta = cliente.get("/operacion/capacidad", headers=OPERACION)
    assert respuesta.status_code == 200 and respuesta.json()["ultimo_escalado"] is None


def test_sin_puntos_de_cpu_las_tareas_activas_son_null(http: Any) -> None:
    cliente, cw, aas = http
    esperar_metricas(cw, {minuto(10): 5.0}, {})
    esperar_limites(aas)
    esperar_actividades(aas, [])
    respuesta = cliente.get("/operacion/capacidad", headers=OPERACION)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert "tareas_activas" in cuerpo and cuerpo["tareas_activas"] is None


@pytest.mark.parametrize(
    "encabezados",
    [
        {},  # sin token: API Gateway responde 401; si llegara al BFF, no trae el grupo
        {"X-Authenticated-Groups": "[administradores-socios]"},
    ],
)
def test_solo_el_grupo_operacion(http: Any, encabezados: dict[str, str]) -> None:
    cliente, cw, aas = http
    respuesta = cliente.get(
        "/operacion/capacidad", headers={**encabezados, "X-Correlation-Id": "w08-capacidad-2"}
    )
    assert respuesta.status_code == 403
    assert respuesta.json() == {
        "codigo": "ROL_NO_AUTORIZADO",
        "mensaje": "No autorizado",
        "correlationId": "w08-capacidad-2",
        "detalles": {},
    }
    cw.assert_no_pending_responses()  # no se consulta AWS
    aas.assert_no_pending_responses()


def test_emisor_que_no_es_del_back_office(http: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cliente, _, _ = http
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": "https://cognito/backoffice"})
    respuesta = cliente.get(
        "/operacion/capacidad",
        headers={**OPERACION, "X-Authenticated-Issuer": "https://cognito/clientes"},
    )
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")


NO_DISPONIBLE = {
    "codigo": "SERVICIO_NO_DISPONIBLE",
    "mensaje": "No se pudo leer la capacidad del servicio",
    "correlationId": "w08-capacidad-1",
    "detalles": {},
}


def _sin_detalle_de_aws(registros: list[logging.LogRecord], tipo: str) -> None:
    errores = [r for r in registros if r.name == "bff_web.operacion"]
    assert [getattr(r, "error_type", None) for r in errores] == [tipo]
    texto = " ".join(f"{r.getMessage()} {vars(r)}" for r in errores)
    assert "Rate exceeded" not in texto and CLUSTER not in texto


def test_sin_configuracion_responde_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app.dependency_overrides[ruta.consultar_capacidad] = lambda: None
    try:
        with caplog.at_level(logging.ERROR, logger="bff_web.operacion"):
            respuesta = TestClient(app).get("/operacion/capacidad", headers=OPERACION)
    finally:
        app.dependency_overrides.clear()
    assert (respuesta.status_code, respuesta.json()) == (503, NO_DISPONIBLE)
    _sin_detalle_de_aws(caplog.records, "CapacidadNoConfigurada")


def test_cloudwatch_no_disponible_responde_503(http: Any, caplog: pytest.LogCaptureFixture) -> None:
    cliente, cw, _ = http
    cw.add_client_error(
        "get_metric_data", service_error_code="Throttling", service_message="Rate exceeded"
    )
    with caplog.at_level(logging.ERROR, logger="bff_web.operacion"):
        respuesta = cliente.get("/operacion/capacidad", headers=OPERACION)
    assert (respuesta.status_code, respuesta.json()) == (503, NO_DISPONIBLE)
    _sin_detalle_de_aws(caplog.records, "ClientError")


def test_auto_scaling_no_disponible_responde_503(
    http: Any, caplog: pytest.LogCaptureFixture
) -> None:
    cliente, cw, aas = http
    esperar_metricas(cw, {}, {})
    aas.add_client_error(
        "describe_scalable_targets",
        service_error_code="AccessDeniedException",
        service_message="Rate exceeded",
        http_status_code=400,
    )
    with caplog.at_level(logging.ERROR, logger="bff_web.operacion"):
        respuesta = cliente.get("/operacion/capacidad", headers=OPERACION)
    assert (respuesta.status_code, respuesta.json()) == (503, NO_DISPONIBLE)
    _sin_detalle_de_aws(caplog.records, "ClientError")


def test_actividades_no_disponibles_responde_503(http: Any) -> None:
    cliente, cw, aas = http
    esperar_metricas(cw, {}, {})
    esperar_limites(aas)
    aas.add_client_error(
        "describe_scaling_activities", service_error_code="InternalServiceException"
    )
    respuesta = cliente.get("/operacion/capacidad", headers=OPERACION)
    assert (respuesta.status_code, respuesta.json()) == (503, NO_DISPONIBLE)


def test_sin_objetivo_de_escalado_responde_503(http: Any) -> None:
    cliente, cw, aas = http
    esperar_metricas(cw, {}, {})
    esperar_limites(aas, hay=False)
    respuesta = cliente.get("/operacion/capacidad", headers=OPERACION)
    assert (respuesta.status_code, respuesta.json()) == (503, NO_DISPONIBLE)
