import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from bff_web.adapters.inbound import operacion as ruta
from bff_web.adapters.inbound.socios import api_socios
from bff_web.adapters.outbound.api_socios import ErrorApiSocios
from bff_web.adapters.outbound.cloudwatch import RepositorioAlarmas, crear_repositorio
from bff_web.application.consultar_alertas import ConsultarAlertas
from bff_web.config import Settings, settings
from bff_web.domain.alertas import (
    Transicion,
    alerta_abierta,
    leer_catalogo,
    leer_umbrales,
    ordenar,
    resumir,
    valor_observado,
)
from bff_web.main import app
from fastapi.testclient import TestClient

AHORA = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
PREFIJO = "solventa-int-"
CATALOGO = leer_catalogo({
    "perfilamiento-p95-advertencia": {"severidad": "advertencia", "titulo": "Perfilamiento p95 por encima del 90 % de la meta", "unidad": "ms", "enlace": {"tipo": "traza", "min_ms": 360}},
    "perfilamiento-p95-critica": {"severidad": "critica", "titulo": "Perfilamiento p95 por encima de la meta", "unidad": "ms", "enlace": {"tipo": "traza", "min_ms": 400}},
    "socio-cuota-advertencia": {"severidad": "advertencia", "titulo": "Socio sobre el 90 % de su cuota", "unidad": "%", "enlace": {"tipo": "socio"}},
    "cola-auditoria-dlq": {"severidad": "informativa", "titulo": "Mensajes en la cola de descarte de auditoria", "unidad": "mensajes", "enlace": {"tipo": "cola", "url": "https://consola/sqs"}},
    "desconocida": {"severidad": "urgente", "titulo": "x", "unidad": "x"},
})
UMBRALES = leer_umbrales([{"metrica": "Perfilamiento p95", "unidad": "ms", "meta": 400, "advertencia": 360, "critica": 400}])


def alarma(nombre: str, minutos: int, umbral: float, reason: dict[str, Any] | None = None, estado: str = "ALARM") -> dict[str, Any]:
    return {
        "AlarmName": f"{PREFIJO}{nombre}", "StateValue": estado, "Threshold": umbral,
        "StateReasonData": json.dumps(reason or {"recentDatapoints": [380.0, 412.5]}),
        "StateUpdatedTimestamp": AHORA, "StateTransitionedTimestamp": AHORA - timedelta(minutes=minutos),
    }


def t(nombre: str, minuto: int, anterior: str, nuevo: str) -> Transicion:
    return Transicion(f"{PREFIJO}{nombre}", AHORA - timedelta(days=1) + timedelta(minutes=minuto), anterior, nuevo)


# --- Dominio ---------------------------------------------------------------------------
@pytest.mark.parametrize(("datos", "esperado"), [
    ('{"recentDatapoints": [950.0, 1000.0], "threshold": 400.0}', 1000.0),
    ('{"evaluatedDatapoints": [{"timestamp": "x", "value": 91.0}]}', 91.0),
    ('{"evaluatedDatapoints": [{"timestamp": "x"}]}', None),
    ("no es json", None),
    (None, None),
])
def test_valor_observado(datos: str | None, esperado: float | None) -> None:
    assert valor_observado(datos) == esperado


def test_alerta_abierta_con_su_catalogo() -> None:
    alerta = alerta_abierta(alarma("perfilamiento-p95-critica", 18, 400), PREFIJO, CATALOGO)
    assert alerta is not None
    assert (alerta.severidad, alerta.titulo, alerta.valor_actual, alerta.umbral, alerta.unidad) == (
        "critica", "Perfilamiento p95 por encima de la meta", 412.5, 400.0, "ms",
    )
    assert alerta.abierta_desde == AHORA - timedelta(minutes=18)
    assert alerta.enlace == {"tipo": "traza", "min_ms": 400}


@pytest.mark.parametrize("cruda", [
    alarma("perfilamiento-p95-critica", 1, 400, estado="OK"),
    alarma("desconocida", 1, 1),
    {**alarma("perfilamiento-p95-critica", 1, 400), "AlarmName": "solventa-prod-perfilamiento-p95-critica"},
])
def test_ignora_alarmas_fuera_del_ambiente_o_del_catalogo(cruda: dict[str, Any]) -> None:
    assert alerta_abierta(cruda, PREFIJO, CATALOGO) is None


def test_sin_fecha_de_transicion_usa_la_de_actualizacion() -> None:
    cruda = alarma("cola-auditoria-dlq", 5, 0)
    del cruda["StateTransitionedTimestamp"]
    assert alerta_abierta(cruda, PREFIJO, CATALOGO).abierta_desde == AHORA


def test_ordena_por_severidad_y_luego_las_mas_recientes() -> None:
    abiertas = [alerta_abierta(alarma(n, m, 1), PREFIJO, CATALOGO) for n, m in [
        ("cola-auditoria-dlq", 6), ("perfilamiento-p95-advertencia", 22), ("socio-cuota-advertencia", 18), ("perfilamiento-p95-critica", 40),
    ]]
    assert [(a.severidad, int((AHORA - a.abierta_desde).total_seconds() // 60)) for a in ordenar(abiertas)] == [
        ("critica", 40), ("advertencia", 18), ("advertencia", 22), ("informativa", 6),
    ]


def test_resumen_del_mes() -> None:
    transiciones = [
        # Advertencia que se recupera a los 3 min: falso positivo.
        t("perfilamiento-p95-advertencia", 0, "OK", "ALARM"), t("perfilamiento-p95-advertencia", 3, "ALARM", "OK"),
        # Crítica de 45 min: meta incumplida (atención > 30 min).
        t("perfilamiento-p95-critica", 10, "OK", "ALARM"), t("perfilamiento-p95-critica", 55, "ALARM", "INSUFFICIENT_DATA"),
        # Crítica de 12 min: dentro de la atención.
        t("perfilamiento-p95-critica", 100, "INSUFFICIENT_DATA", "ALARM"), t("perfilamiento-p95-critica", 112, "ALARM", "OK"),
        # Salida de ALARM sin su entrada en el periodo: pertenece al mes anterior.
        t("cola-auditoria-dlq", 1, "ALARM", "OK"),
        # Transiciones sin ALARM, de otros ambientes o fuera del catálogo: no cuentan.
        t("cola-auditoria-dlq", 2, "INSUFFICIENT_DATA", "OK"),
        Transicion("solventa-prod-perfilamiento-p95-critica", AHORA - timedelta(hours=2), "OK", "ALARM"),
        t("desconocida", 5, "OK", "ALARM"),
        # Sigue abierta.
        t("socio-cuota-advertencia", 200, "OK", "ALARM"),
    ]
    resumen = resumir(reversed(transiciones), CATALOGO, PREFIJO, AHORA - timedelta(days=30), AHORA, 5, 30)
    assert (resumen.generadas, resumen.criticas, resumen.falsos_positivos, resumen.tiempo_medio_atencion_min, resumen.metas_incumplidas) == (
        4, 2, 1, 20, 1,
    )


def test_resumen_sin_alertas() -> None:
    resumen = resumir([], CATALOGO, PREFIJO, AHORA - timedelta(days=30), AHORA, 5, 30)
    assert (resumen.generadas, resumen.tiempo_medio_atencion_min, resumen.metas_incumplidas) == (0, None, 0)


def test_critica_abierta_mas_alla_de_la_atencion_es_meta_incumplida() -> None:
    resumen = resumir([t("perfilamiento-p95-critica", 0, "OK", "ALARM")], CATALOGO, PREFIJO, AHORA - timedelta(days=30), AHORA, 5, 30)
    assert (resumen.criticas, resumen.metas_incumplidas, resumen.tiempo_medio_atencion_min) == (1, 1, None)


# --- Adaptador CloudWatch --------------------------------------------------------------
class CloudWatchFalso:
    def __init__(self, paginas: dict[str, list[dict[str, Any]]]) -> None:
        self.paginas = paginas
        self.llamadas: list[tuple[str, dict[str, Any]]] = []

    def _pagina(self, operacion: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.llamadas.append((operacion, kwargs))
        return self.paginas[operacion][int(kwargs.get("NextToken", 0))]

    def describe_alarms(self, **kwargs: Any) -> dict[str, Any]:
        return self._pagina("describe_alarms", kwargs)

    def describe_alarm_history(self, **kwargs: Any) -> dict[str, Any]:
        return self._pagina("describe_alarm_history", kwargs)

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        return self._pagina("get_metric_data", kwargs)


def historia(nombre: str, anterior: str, nuevo: str) -> dict[str, Any]:
    return {
        "AlarmName": nombre, "Timestamp": AHORA,
        "HistoryData": json.dumps({"oldState": {"stateValue": anterior}, "newState": {"stateValue": nuevo}}),
    }


def test_abiertas_pagina_y_filtra_por_prefijo_y_estado() -> None:
    cliente = CloudWatchFalso({"describe_alarms": [
        {"MetricAlarms": [{"AlarmName": "a"}], "NextToken": "1"}, {"MetricAlarms": [{"AlarmName": "b"}]},
    ]})
    assert [a["AlarmName"] for a in RepositorioAlarmas(cliente, "int").abiertas(PREFIJO)] == ["a", "b"]
    assert cliente.llamadas[0][1] == {"AlarmNamePrefix": PREFIJO, "StateValue": "ALARM", "AlarmTypes": ["MetricAlarm"]}
    assert cliente.llamadas[1][1]["NextToken"] == "1"


def test_transiciones_del_ambiente() -> None:
    cliente = CloudWatchFalso({"describe_alarm_history": [
        {"AlarmHistoryItems": [historia(f"{PREFIJO}x", "OK", "ALARM"), historia("solventa-prod-x", "OK", "ALARM")], "NextToken": "1"},
        {"AlarmHistoryItems": [{"AlarmName": f"{PREFIJO}x", "Timestamp": AHORA}]},
    ]})
    desde = AHORA - timedelta(days=30)
    transiciones = RepositorioAlarmas(cliente, "int").transiciones(PREFIJO, desde, AHORA)
    assert transiciones == [Transicion(f"{PREFIJO}x", AHORA, "OK", "ALARM"), Transicion(f"{PREFIJO}x", AHORA, "", "")]
    parametros = cliente.llamadas[0][1]
    assert (parametros["HistoryItemType"], parametros["StartDate"], parametros["EndDate"]) == ("StateUpdate", desde, AHORA)


def test_socio_con_mayor_cuota() -> None:
    cliente = CloudWatchFalso({"get_metric_data": [{"MetricDataResults": [{"Label": "SOC-000007", "Values": [92.0]}]}]})
    repositorio = RepositorioAlarmas(cliente, "int", ahora=lambda: AHORA)
    assert repositorio.socio_con_mayor_cuota() == "SOC-000007"
    consulta = cliente.llamadas[0][1]["MetricDataQueries"][0]["Expression"]
    assert "MAX(PartnerQuotaUsage)" in consulta and "Environment = 'int'" in consulta and "LIMIT 1" in consulta
    sin_datos = CloudWatchFalso({"get_metric_data": [{"MetricDataResults": [{"Label": "x", "Values": []}]}]})
    assert RepositorioAlarmas(sin_datos, "int").socio_con_mayor_cuota() is None


def test_fabrica_boto3() -> None:
    repositorio = crear_repositorio("us-east-1", "int")
    assert repositorio.cliente.meta.service_model.service_name == "cloudwatch" and repositorio.ambiente == "int"


# --- Caso de uso y ruta -----------------------------------------------------------------
class RepositorioEnMemoria:
    def __init__(self, abiertas: list[dict[str, Any]], transiciones: list[Transicion], socio: str | None = "SOC-000007") -> None:
        self._abiertas = abiertas
        self._transiciones = transiciones
        self.socio = socio
        self.falla = False

    def abiertas(self, prefijo: str) -> list[dict[str, Any]]:
        if self.falla:
            raise ConnectionError("cloudwatch no disponible")
        return self._abiertas

    def transiciones(self, prefijo: str, desde: datetime, hasta: datetime) -> list[Transicion]:
        assert hasta - desde == timedelta(days=30)
        return self._transiciones

    def socio_con_mayor_cuota(self) -> str | None:
        return self.socio


class SociosFalsos:
    def __init__(self, falla: bool = False) -> None:
        self.falla = falla

    async def obtener(self, socio_id: str, administrador: str | None) -> dict[str, Any]:
        if self.falla:
            raise ErrorApiSocios(404, "SOCIO_NO_ENCONTRADO", None, {})
        return {"id": socio_id, "nombre": "Retail Andino"}


OPERACION = {"X-Authenticated-Groups": "[operacion]"}


def caso(repositorio: RepositorioEnMemoria) -> ConsultarAlertas:
    return ConsultarAlertas(repositorio, PREFIJO, CATALOGO, UMBRALES, 5, 30, ahora=lambda: AHORA)


@pytest.fixture
def cliente() -> Any:
    repositorio = RepositorioEnMemoria(
        [alarma("socio-cuota-advertencia", 22, 90, {"recentDatapoints": [91.0]}), alarma("cola-auditoria-dlq", 6, 0, {"recentDatapoints": [3.0]}),
         alarma("perfilamiento-p95-advertencia", 18, 360)],
        [t("perfilamiento-p95-critica", 0, "OK", "ALARM"), t("perfilamiento-p95-critica", 11, "ALARM", "OK")],
    )
    socios = SociosFalsos()
    app.dependency_overrides[ruta.consultar_alertas] = lambda: caso(repositorio)
    app.dependency_overrides[api_socios] = lambda: socios
    yield TestClient(app), repositorio, socios
    app.dependency_overrides.clear()


def test_panel_de_alertas(cliente: Any) -> None:
    http, _, _ = cliente
    respuesta = http.get("/operacion/alertas", headers=OPERACION)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert (cuerpo["ambiente"], cuerpo["porcentaje_advertencia"], cuerpo["atencion_min"]) == (settings.environment, 90, 30)
    assert [(a["severidad"], a["titulo"], a["valor_actual"], a["umbral"]) for a in cuerpo["abiertas"]] == [
        ("advertencia", "Perfilamiento p95 por encima del 90 % de la meta", 412.5, 360.0),
        ("advertencia", "Socio Retail Andino sobre el 90 % de su cuota", 91.0, 90.0),
        ("informativa", "Mensajes en la cola de descarte de auditoria", 3.0, 0.0),
    ]
    assert cuerpo["abiertas"][1]["enlace"] == {"tipo": "socio", "url": None, "socio_id": "SOC-000007", "min_ms": None}
    assert cuerpo["abiertas"][0]["enlace"]["min_ms"] == 360
    assert cuerpo["umbrales"] == [{"metrica": "Perfilamiento p95", "unidad": "ms", "meta": 400, "advertencia": 360, "critica": 400}]
    assert {k: cuerpo["resumen"][k] for k in ("generadas", "criticas", "falsos_positivos", "tiempo_medio_atencion_min", "metas_incumplidas")} == {
        "generadas": 1, "criticas": 1, "falsos_positivos": 0, "tiempo_medio_atencion_min": 11, "metas_incumplidas": 0,
    }


def test_sin_nombre_del_socio_muestra_su_identificador(cliente: Any) -> None:
    http, _, socios = cliente
    socios.falla = True
    titulos = [a["titulo"] for a in http.get("/operacion/alertas", headers=OPERACION).json()["abiertas"]]
    assert "Socio SOC-000007 sobre el 90 % de su cuota" in titulos


def test_sin_socio_identificado_conserva_el_titulo(cliente: Any) -> None:
    http, repositorio, _ = cliente
    repositorio.socio = None
    alerta = next(a for a in http.get("/operacion/alertas", headers=OPERACION).json()["abiertas"] if a["enlace"]["tipo"] == "socio")
    assert (alerta["titulo"], alerta["enlace"]["socio_id"]) == ("Socio sobre el 90 % de su cuota", None)


def test_cloudwatch_no_disponible_responde_502(cliente: Any) -> None:
    http, repositorio, _ = cliente
    repositorio.falla = True
    assert http.get("/operacion/alertas", headers=OPERACION).status_code == 502


@pytest.mark.parametrize("encabezados", [{}, {"X-Authenticated-Groups": "[administradores-socios]"}])
def test_solo_el_grupo_operacion(cliente: Any, encabezados: dict[str, str]) -> None:
    http, _, _ = cliente
    assert http.get("/operacion/alertas", headers=encabezados).status_code == 403


def test_configuracion_desde_terraform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERTAS_PREFIJO", PREFIJO)
    monkeypatch.setenv("ALERTAS_CATALOGO", json.dumps({"rds-cpu": {"severidad": "advertencia", "titulo": "CPU", "unidad": "%"}}))
    monkeypatch.setenv("ALERTAS_UMBRALES", json.dumps([{"metrica": "Disponibilidad", "unidad": "%", "meta": 99.9, "advertencia": 99.93, "critica": 99.9}]))
    monkeypatch.setenv("ALERTAS_ADVERTENCIA_PCT", "90")
    monkeypatch.setenv("ALERTAS_ATENCION_MIN", "45")
    monkeypatch.setenv("ALERTAS_FALSO_POSITIVO_MIN", "2")
    leida = Settings()
    assert leida.alertas_prefijo == PREFIJO and leida.alertas_catalogo["rds-cpu"]["severidad"] == "advertencia"
    assert leer_umbrales(leida.alertas_umbrales)[0].advertencia == 99.93
    assert (leida.alertas_atencion_min, leida.alertas_falso_positivo_min) == (45, 2)


def test_construye_el_caso_con_la_configuracion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "prueba")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "prueba")
    monkeypatch.setattr(settings, "alertas_catalogo", {"rds-cpu": {"severidad": "advertencia", "titulo": "CPU", "unidad": "%"}})
    ruta.consultar_alertas.cache_clear()
    try:
        construido = ruta.consultar_alertas()
        assert isinstance(construido.repositorio, RepositorioAlarmas) and list(construido.catalogo) == ["rds-cpu"]
    finally:
        ruta.consultar_alertas.cache_clear()
