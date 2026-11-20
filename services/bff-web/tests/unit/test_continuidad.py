"""HU-W29: Operación › Continuidad (I04) y Conciliación (I05) con CloudWatch, Logs y S3 simulados.

El incidente sintético es el del experimento: la zona A pierde la red, ECS retira sus tareas, RDS
promueve el standby de la zona B, la alarma de 5XX se abre y se recupera.
"""

import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from bff_web.adapters.inbound import operacion as ruta
from bff_web.adapters.outbound.continuidad_aws import (
    CONSULTA_TAREAS,
    ErrorConsulta,
    RepositorioEventos,
    RepositorioInformes,
    RepositorioTrafico,
    crear_repositorios,
)
from bff_web.application.consultar_continuidad import ConsultarConciliacion, ConsultarContinuidad
from bff_web.config import Settings, settings
from bff_web.domain.alertas import Transicion
from bff_web.domain.continuidad import (
    Base,
    EstadoTarea,
    base,
    cronologia,
    estado_general,
    indicadores,
    inicio_incidente,
    recorridos,
    zonas,
)
from bff_web.main import app
from fastapi.testclient import TestClient

AHORA = datetime(2026, 11, 20, 14, 30, tzinfo=UTC)
INYECCION = datetime(2026, 11, 20, 14, 5, tzinfo=UTC)
PREFIJO = "solventa-exp-"
A, B = "us-east-1a", "us-east-1b"
RECORRIDOS = {
    "R1": {"nombre": "Cotización", "alarmas": ["cotizacion-errores-critica", "api-canales-5xx"]},
    "R3": {"nombre": "Suscripción, aceptación y recaudo", "alarmas": ["polizas-errores-critica"]},
}
TITULOS = {"api-canales-5xx": "Errores 5XX del API canales sobre 5.0 %"}


def evento(tipo: str, minutos: float, **detalle: Any) -> dict[str, Any]:
    instante = INYECCION + timedelta(minutes=minutos)
    return {"detail-type": tipo, "time": instante.strftime("%Y-%m-%dT%H:%M:%SZ"), "detail": detalle}


def tarea(
    minutos: float, arn: str, zona: str, estado: str, deseado: str = "RUNNING"
) -> dict[str, Any]:
    return evento(
        "ECS Task State Change",
        minutos,
        taskArn=arn,
        availabilityZone=zona,
        lastStatus=estado,
        desiredStatus=deseado,
    )


EVENTOS = [
    evento(
        "FIS Experiment State Change",
        0,
        **{"experiment-id": "EXP1", "new-state": {"status": "running"}},
    ),
    evento(
        "RDS DB Instance Event",
        0.5,
        EventID="RDS-EVENT-0013",
        Message="Multi-AZ instance failover started.",
    ),
    tarea(0.6, "t1", A, "STOPPED", "STOPPED"),
    tarea(0.7, "t2", A, "STOPPED", "STOPPED"),
    tarea(0.7, "t2", A, "STOPPED", "STOPPED"),  # el mismo cambio repetido cuenta una vez
    evento(
        "RDS DB Instance Event",
        1.2,
        EventID="RDS-EVENT-0049",
        Message="Multi-AZ instance failover completed.",
    ),
    tarea(2.1, "t5", B, "RUNNING"),
    tarea(2.2, "t6", B, "PENDING"),
    evento("ECS Service Action", 3, eventName="SERVICE_STEADY_STATE"),
    evento(
        "FIS Experiment State Change",
        10,
        **{"experiment-id": "EXP1", "new-state": {"status": "completed"}},
    ),
]
TRANSICIONES = [
    Transicion(f"{PREFIJO}api-canales-5xx", INYECCION + timedelta(minutes=2), "OK", "ALARM"),
    Transicion(f"{PREFIJO}api-canales-5xx", INYECCION + timedelta(minutes=6), "ALARM", "OK"),
    Transicion(f"{PREFIJO}otra", INYECCION + timedelta(minutes=7), "INSUFFICIENT_DATA", "OK"),
]
INFORME = {
    "version": 1,
    "experimento": "EXP1",
    "generado": "2026-11-20T14:40:00Z",
    "ventana": {"desde": "2026-11-20T14:00:00Z", "hasta": "2026-11-20T14:30:00Z"},
    "estado": "SIN_DIFERENCIAS",
    "resumen": {
        "confirmadas": 1290,
        "perdidas": 0,
        "duplicadas": 0,
        "cobrosDuplicados": 0,
        "dlqSinExplicacion": 0,
        "pendientesOutbox": 0,
        "solicitudes": 1400,
        "solicitudesConError": 12,
        "porcentajeError": 0.86,
    },
    "verificaciones": [
        {
            "id": "cotizaciones",
            "descripcion": "Cotizaciones confirmadas (R1 y R3) registradas una vez",
            "esperado": 1100,
            "observado": 1100,
            "resultado": "COINCIDE",
            "detalle": [],
        },
    ],
    "recorridos": {
        "R1": {
            "intentos": 1200,
            "confirmadas": 1100,
            "errores": 10,
            "erroresPorEstado": {"503": 10},
        },
        "R5": {"estado": "PENDIENTE_HU_W21_W22"},
    },
    "continuidad": {
        "inyeccion": "2026-11-20T14:05:00Z",
        "redireccionSegundos": 38,
        "objetivoSegundos": 600,
        "cumpleObjetivo": True,
        "serie": [],
    },
    "dlq": {"total": 0, "explicados": []},
}


# --- Dominio --------------------------------------------------------------------------
def test_zonas_con_tareas_saludables_esperadas_y_trafico() -> None:
    tareas = [
        EstadoTarea(A, "RUNNING", "RUNNING", "HEALTHY"),
        EstadoTarea(A, "RUNNING", "RUNNING", "UNHEALTHY"),
        EstadoTarea(B, "RUNNING", "RUNNING"),
        EstadoTarea(B, "RUNNING", "RUNNING"),
        EstadoTarea(B, "STOPPED", "STOPPED"),
        EstadoTarea("", "RUNNING", "RUNNING"),
    ]
    resultado = zonas(tareas, {A: 25.0, B: 75.0})
    assert [
        (z.zona, z.tareas_saludables, z.tareas_esperadas, z.trafico_pct, z.estado)
        for z in resultado
    ] == [
        (A, 1, 2, 25.0, "DEGRADADA"),
        (B, 2, 2, 75.0, "SALUDABLE"),
    ]
    assert estado_general(resultado) == "DOS_ZONAS"


def test_zona_sin_trafico_retirada_y_sin_tareas_no_disponible() -> None:
    tareas = [EstadoTarea(A, "RUNNING", "RUNNING"), EstadoTarea(B, "RUNNING", "RUNNING")]
    retirada = zonas(tareas, {A: 0.0, B: 120.0})
    assert [z.estado for z in retirada] == ["RETIRADA", "SALUDABLE"]
    assert estado_general(retirada) == "UNA_ZONA"
    caida = zonas([EstadoTarea(A, "PENDING", "RUNNING"), EstadoTarea(B, "RUNNING", "RUNNING")], {})
    assert [(z.estado, z.trafico_pct) for z in caida] == [
        ("NO_DISPONIBLE", None),
        ("SALUDABLE", None),
    ]
    assert estado_general([]) == "SIN_DATOS"


def test_base_segun_el_ultimo_evento_de_failover() -> None:
    assert base([]) == Base("MULTI_AZ")
    assert base(EVENTOS[:2]) == Base("FAILOVER_EN_CURSO", INYECCION + timedelta(seconds=30))
    assert base(EVENTOS) == Base("STANDBY_PROMOVIDO", INYECCION + timedelta(seconds=72))


def test_recorrido_degradado_solo_con_sus_alarmas_abiertas() -> None:
    resultado = recorridos(RECORRIDOS, [f"{PREFIJO}api-canales-5xx", f"{PREFIJO}rds-cpu"], PREFIJO)
    assert [(r.id, r.estado, r.alarmas_abiertas) for r in resultado] == [
        ("R1", "DEGRADADO", ["api-canales-5xx"]),
        ("R3", "OPERANDO", []),
    ]


def test_cronologia_del_incidente() -> None:
    hitos = cronologia(EVENTOS, TRANSICIONES, TITULOS, PREFIJO)
    assert [(h.fuente, h.tipo, h.descripcion, h.cantidad) for h in hitos] == [
        ("FIS", "INYECCION", "Inicio de la falla controlada EXP1", None),
        ("RDS", "FAILOVER_INICIADO", "Multi-AZ instance failover started.", None),
        ("ECS", "TAREAS_DETENIDAS", f"2 tareas retiradas en {A}", 2),
        ("RDS", "FAILOVER_COMPLETADO", "Multi-AZ instance failover completed.", None),
        ("ALARMA", "ALARMA", "Errores 5XX del API canales sobre 5.0 %", None),
        ("ECS", "TAREAS_INICIADAS", f"1 tareas en ejecución en {B}", 1),
        ("ALARMA", "RECUPERADA", "Errores 5XX del API canales sobre 5.0 %", None),
        ("FIS", "FIN_INYECCION", "Fin de la falla controlada (completed)", None),
    ]
    assert (hitos[2].zona, hitos[2].instante) == (A, INYECCION + timedelta(seconds=36))
    assert inicio_incidente(hitos) == INYECCION


def test_sin_experimento_el_incidente_empieza_con_la_primera_alarma() -> None:
    hitos = cronologia([], TRANSICIONES, {}, PREFIJO)
    assert hitos[0].descripcion == "api-canales-5xx"
    assert inicio_incidente(hitos) == INYECCION + timedelta(minutes=2)
    assert inicio_incidente([]) is None


def test_evento_de_rds_sin_failover_y_estados_de_fis_ignorados() -> None:
    otros = [
        evento(
            "RDS DB Instance Event", 1, EventID="RDS-EVENT-0006", Message="DB instance restarted"
        ),
        evento("FIS Experiment State Change", 0, **{"new-state": {"status": "initiating"}}),
        tarea(1, "t9", A, "PROVISIONING"),
    ]
    assert [(h.tipo, h.descripcion) for h in cronologia(otros, [], {}, PREFIJO)] == [
        ("EVENTO_BASE", "DB instance restarted")
    ]


def test_indicadores_desde_el_informe() -> None:
    resultado = indicadores(INFORME)
    assert resultado is not None
    assert (
        resultado.transacciones_perdidas,
        resultado.cobros_duplicados,
        resultado.redireccion_s,
        resultado.porcentaje_error,
    ) == (0, 0, 38, 0.86)
    assert (
        resultado.experimento,
        resultado.estado_conciliacion,
        resultado.objetivo_redireccion_s,
    ) == ("EXP1", "SIN_DIFERENCIAS", 600)
    assert indicadores(None) is None and indicadores({"version": 2}) is None
    sin_continuidad = indicadores({**INFORME, "continuidad": None})
    assert sin_continuidad is not None and sin_continuidad.redireccion_s is None


# --- Adaptadores --------------------------------------------------------------------------
class LogsFalso:
    def __init__(self, estados: list[str], paginas: list[dict[str, Any]] | None = None) -> None:
        self.estados = estados
        self.paginas = paginas or []
        self.consultas: list[dict[str, Any]] = []
        self.filtros: list[dict[str, Any]] = []

    def start_query(self, **parametros: Any) -> dict[str, str]:
        self.consultas.append(parametros)
        return {"queryId": "q1"}

    def get_query_results(self, queryId: str) -> dict[str, Any]:
        estado = self.estados.pop(0)
        fila = [
            {"field": "detail.taskArn", "value": "t1"},
            {"field": "zona", "value": A},
            {"field": "estado", "value": "RUNNING"},
            {"field": "deseado", "value": "RUNNING"},
        ]
        return {"status": estado, "results": [fila] if estado == "Complete" else []}

    def filter_log_events(self, **parametros: Any) -> dict[str, Any]:
        self.filtros.append(parametros)
        return self.paginas.pop(0)


def test_tareas_con_logs_insights_esperando_el_resultado() -> None:
    logs = LogsFalso(["Running", "Complete"])
    esperas: list[float] = []
    repositorio = RepositorioEventos(
        logs, "/ecs/solventa-exp/events", ahora=lambda: 1_000_000.0, dormir=esperas.append
    )

    assert repositorio.tareas() == [EstadoTarea(A, "RUNNING", "RUNNING", "UNKNOWN")]
    assert esperas == [0.5]
    consulta = logs.consultas[0]
    assert (
        consulta["logGroupName"],
        consulta["queryString"],
        consulta["endTime"] - consulta["startTime"],
    ) == (
        "/ecs/solventa-exp/events",
        CONSULTA_TAREAS,
        14 * 86400,
    )


@pytest.mark.parametrize("estados", [["Failed"], ["Running", "Running", "Running"]])
def test_consulta_fallida_o_vencida(estados: list[str]) -> None:
    reloj = iter([0.0, 0.0, 5.0, 9.0, 20.0])
    repositorio = RepositorioEventos(
        LogsFalso(estados), "g", ahora=lambda: next(reloj), dormir=lambda _: None
    )
    with pytest.raises(ErrorConsulta):
        repositorio.tareas()


def test_eventos_de_la_ventana_paginados_y_sin_mensajes_ilegibles() -> None:
    logs = LogsFalso(
        [],
        [
            {
                "events": [{"message": json.dumps(EVENTOS[0])}, {"message": "no es json"}],
                "nextToken": "p2",
            },
            {"events": [{"message": json.dumps(EVENTOS[1])}, {}]},
        ],
    )
    repositorio = RepositorioEventos(logs, "g")

    assert repositorio.eventos(INYECCION, AHORA) == EVENTOS[:2]
    assert logs.filtros[0]["startTime"] == int(INYECCION.timestamp() * 1000)
    assert logs.filtros[1]["nextToken"] == "p2"


class CloudWatchFalso:
    def __init__(self) -> None:
        self.llamadas: list[dict[str, Any]] = []

    def get_metric_data(self, **parametros: Any) -> dict[str, Any]:
        self.llamadas.append(parametros)
        return {
            "MetricDataResults": [
                {"Label": f"app/solventa-exp-access/1 {A}", "Values": [10.0, 5.0]},
                {"Label": f"{B} app/solventa-exp-access/1", "Values": [45.0]},
                {"Label": "sin zona", "Values": [99.0]},
            ]
        }


def test_trafico_del_alb_por_zona() -> None:
    cloudwatch = CloudWatchFalso()
    trafico = RepositorioTrafico(cloudwatch, "app/solventa-exp-access/1").por_zona(
        AHORA - timedelta(minutes=5), AHORA
    )
    assert trafico == {A: 15.0, B: 45.0}
    expresion = cloudwatch.llamadas[0]["MetricDataQueries"][0]["Expression"]
    assert 'LoadBalancer="app/solventa-exp-access/1"' in expresion and "RequestCount" in expresion
    assert RepositorioTrafico(cloudwatch, "").por_zona(AHORA, AHORA) == {}


class S3Falso:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self, cuerpo: dict[str, Any] | None) -> None:
        self.cuerpo = cuerpo
        self.pedidos: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        self.pedidos.append((Bucket, Key))
        if self.cuerpo is None:
            raise self.exceptions.NoSuchKey()
        return {"Body": io.BytesIO(json.dumps(self.cuerpo).encode())}


def test_informe_del_bucket_de_experimentos() -> None:
    s3 = S3Falso(INFORME)
    assert RepositorioInformes(s3, "solventa-exp-experimentos-1").ultimo() == INFORME
    assert s3.pedidos == [("solventa-exp-experimentos-1", "hu-w29/informe.json")]
    assert RepositorioInformes(S3Falso(None), "b").ultimo() is None
    # Fuera de exp no hay bucket: no se llama a S3.
    sin_bucket = S3Falso(INFORME)
    assert RepositorioInformes(sin_bucket, "").ultimo() is None and sin_bucket.pedidos == []


def test_fabrica_boto3() -> None:
    eventos, trafico, informes = crear_repositorios("us-east-1", "g", "app/x/1", "b")
    assert (
        eventos.cliente.meta.service_model.service_name,
        trafico.cliente.meta.service_model.service_name,
        informes.cliente.meta.service_model.service_name,
    ) == ("logs", "cloudwatch", "s3")


# --- Caso de uso y rutas --------------------------------------------------------------
class EventosEnMemoria:
    def __init__(self) -> None:
        self.falla = False
        self.ventanas: list[tuple[datetime, datetime]] = []

    def tareas(self) -> list[EstadoTarea]:
        if self.falla:
            raise ErrorConsulta("Timeout")
        return [
            EstadoTarea(A, "PENDING", "RUNNING"),
            EstadoTarea(B, "RUNNING", "RUNNING"),
            EstadoTarea(B, "RUNNING", "RUNNING"),
        ]

    def eventos(self, desde: datetime, hasta: datetime) -> list[dict[str, Any]]:
        self.ventanas.append((desde, hasta))
        return EVENTOS


class TraficoEnMemoria:
    def por_zona(self, desde: datetime, hasta: datetime) -> dict[str, float]:
        assert hasta - desde == timedelta(minutes=5)
        return {A: 0.0, B: 300.0}


class AlarmasEnMemoria:
    def abiertas(self, prefijo: str) -> list[dict[str, Any]]:
        return [{"AlarmName": f"{prefijo}api-canales-5xx"}]

    def transiciones(self, prefijo: str, desde: datetime, hasta: datetime) -> list[Transicion]:
        return TRANSICIONES


class InformesEnMemoria:
    def __init__(self, informe: dict[str, Any] | None = INFORME) -> None:
        self.informe = informe
        self.falla = False

    def ultimo(self) -> dict[str, Any] | None:
        if self.falla:
            raise ConnectionError("s3 no disponible")
        return self.informe


OPERACION = {"X-Authenticated-Groups": "[operacion]"}


@pytest.fixture
def cliente() -> Any:
    eventos, informes = EventosEnMemoria(), InformesEnMemoria()
    continuidad = ConsultarContinuidad(
        eventos,
        TraficoEnMemoria(),
        AlarmasEnMemoria(),
        informes,
        PREFIJO,
        RECORRIDOS,
        TITULOS,
        ahora=lambda: AHORA,
    )
    app.dependency_overrides[ruta.consultar_continuidad] = lambda: continuidad
    app.dependency_overrides[ruta.consultar_conciliacion] = lambda: ConsultarConciliacion(informes)
    yield TestClient(app), eventos, informes
    app.dependency_overrides.clear()


def test_continuidad_durante_el_incidente(cliente: Any) -> None:
    http, eventos, _ = cliente
    respuesta = http.get("/operacion/continuidad", params={"horas": 2}, headers=OPERACION)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert (cuerpo["estado"], cuerpo["incidente_desde"]) == ("UNA_ZONA", "2026-11-20T14:05:00Z")
    assert cuerpo["zonas"] == [
        {
            "zona": A,
            "tareas_saludables": 0,
            "tareas_esperadas": 1,
            "trafico_pct": 0.0,
            "estado": "NO_DISPONIBLE",
        },
        {
            "zona": B,
            "tareas_saludables": 2,
            "tareas_esperadas": 2,
            "trafico_pct": 100.0,
            "estado": "SALUDABLE",
        },
    ]
    assert cuerpo["base"] == {"estado": "STANDBY_PROMOVIDO", "desde": "2026-11-20T14:06:12Z"}
    assert [(r["id"], r["estado"]) for r in cuerpo["recorridos"]] == [
        ("R1", "DEGRADADO"),
        ("R3", "OPERANDO"),
    ]
    assert [h["tipo"] for h in cuerpo["cronologia"]][:3] == [
        "INYECCION",
        "FAILOVER_INICIADO",
        "TAREAS_DETENIDAS",
    ]
    assert cuerpo["indicadores"] == {
        "experimento": "EXP1",
        "estado_conciliacion": "SIN_DIFERENCIAS",
        "transacciones_perdidas": 0,
        "cobros_duplicados": 0,
        "redireccion_s": 38,
        "objetivo_redireccion_s": 600,
        "porcentaje_error": 0.86,
    }
    assert eventos.ventanas == [(AHORA - timedelta(hours=2), AHORA)]


def test_sin_informe_la_continuidad_sigue_sin_indicadores(cliente: Any) -> None:
    http, _, informes = cliente
    informes.falla = True
    assert http.get("/operacion/continuidad", headers=OPERACION).json()["indicadores"] is None
    informes.falla, informes.informe = False, None
    assert http.get("/operacion/continuidad", headers=OPERACION).json()["indicadores"] is None


def test_cloudwatch_no_disponible_responde_502(cliente: Any) -> None:
    http, eventos, _ = cliente
    eventos.falla = True
    assert http.get("/operacion/continuidad", headers=OPERACION).status_code == 502


@pytest.mark.parametrize("horas", [0, 25])
def test_ventana_fuera_de_rango(cliente: Any, horas: int) -> None:
    http, _, _ = cliente
    assert (
        http.get("/operacion/continuidad", params={"horas": horas}, headers=OPERACION).status_code
        == 422
    )


def test_conciliacion_devuelve_el_informe_tal_cual(cliente: Any) -> None:
    http, _, _ = cliente
    respuesta = http.get("/operacion/continuidad/conciliacion", headers=OPERACION)
    assert respuesta.status_code == 200
    assert respuesta.json() == INFORME


def test_conciliacion_sin_informe_404_y_s3_caido_o_formato_distinto_502(cliente: Any) -> None:
    http, _, informes = cliente
    informes.informe = None
    assert http.get("/operacion/continuidad/conciliacion", headers=OPERACION).status_code == 404
    informes.informe = {**INFORME, "version": 2}
    assert http.get("/operacion/continuidad/conciliacion", headers=OPERACION).status_code == 502
    informes.falla = True
    assert http.get("/operacion/continuidad/conciliacion", headers=OPERACION).status_code == 502


@pytest.mark.parametrize("ruta_", ["/operacion/continuidad", "/operacion/continuidad/conciliacion"])
@pytest.mark.parametrize("encabezados", [{}, {"X-Authenticated-Groups": "[asesores]"}])
def test_solo_el_grupo_operacion(cliente: Any, ruta_: str, encabezados: dict[str, str]) -> None:
    http, _, _ = cliente
    assert http.get(ruta_, headers=encabezados).status_code == 403


def test_configuracion_desde_terraform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTINUIDAD_EVENTOS", "/ecs/solventa-exp/events")
    monkeypatch.setenv("CONTINUIDAD_ALB", "app/solventa-exp-access/1")
    monkeypatch.setenv("CONTINUIDAD_RECORRIDOS", json.dumps(RECORRIDOS))
    monkeypatch.setenv("CONTINUIDAD_BUCKET", "solventa-exp-experimentos-1")
    leida = Settings()
    assert (leida.continuidad_eventos, leida.continuidad_alb, leida.continuidad_bucket) == (
        "/ecs/solventa-exp/events",
        "app/solventa-exp-access/1",
        "solventa-exp-experimentos-1",
    )
    assert leida.continuidad_recorridos["R1"]["alarmas"] == [
        "cotizacion-errores-critica",
        "api-canales-5xx",
    ]


def test_construye_los_casos_con_la_configuracion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "prueba")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "prueba")
    monkeypatch.setattr(
        settings,
        "alertas_catalogo",
        {"api-canales-5xx": {"titulo": "5XX", "severidad": "critica", "unidad": "%"}},
    )
    monkeypatch.setattr(settings, "continuidad_recorridos", RECORRIDOS)
    monkeypatch.setattr(settings, "continuidad_bucket", "b")
    for fabrica in (
        ruta._repositorios_continuidad,
        ruta.consultar_continuidad,
        ruta.consultar_conciliacion,
    ):
        fabrica.cache_clear()
    try:
        continuidad = ruta.consultar_continuidad()
        assert (continuidad.titulos, continuidad.recorridos) == (
            {"api-canales-5xx": "5XX"},
            RECORRIDOS,
        )
        assert (
            isinstance(continuidad.informes, RepositorioInformes)
            and continuidad.informes.bucket == "b"
        )
        assert ruta.consultar_conciliacion().informes is continuidad.informes
    finally:
        for fabrica in (
            ruta._repositorios_continuidad,
            ruta.consultar_continuidad,
            ruta.consultar_conciliacion,
        ):
            fabrica.cache_clear()
