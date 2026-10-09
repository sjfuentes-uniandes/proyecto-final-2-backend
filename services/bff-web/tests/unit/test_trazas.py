import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from bff_web.adapters.inbound import operacion as ruta
from bff_web.adapters.outbound.xray import RepositorioXRay, expresion_filtro
from bff_web.application.consultar_traza import ConsultarTraza
from bff_web.domain.trazas import (
    ESTADO_CORRECTO,
    ESTADO_ERROR,
    FiltroTrazas,
    construir_detalle,
    formatear_ms,
    formatear_segundos,
    interpretar,
)
from bff_web.main import app
from fastapi.testclient import TestClient

T0 = 1_791_500_000.0
TRACE_ID = "1-6a2b3c4d-0123456789abcdef01234567"


def documentos(error_en_aliado: bool = False) -> list[dict[str, Any]]:
    """api-socios -> cotizacion -> simulador-aliados (externo), como los entrega BatchGetTraces."""
    socios = {
        "id": "s1", "name": "api-socios", "trace_id": TRACE_ID, "start_time": T0, "end_time": T0 + 0.800,
        "http": {"request": {"method": "POST", "url": "http://api-socios:8080/socios/cotizaciones?token=x"}},
        "annotations": {"correlation_id": "prueba-123"},
        "subsegments": [{
            "id": "s1-op", "name": "solicitar_cotizacion", "start_time": T0 + 0.005, "end_time": T0 + 0.790,
            "annotations": {"operation": "solicitar_cotizacion", "correlation_id": "prueba-123"},
            "subsegments": [{
                "id": "s1-http", "name": "cotizacion", "namespace": "remote",
                "start_time": T0 + 0.050, "end_time": T0 + 0.750,
            }],
        }],
    }
    cotizacion = {
        "id": "c1", "name": "cotizacion", "trace_id": TRACE_ID, "parent_id": "s1-http",
        "start_time": T0 + 0.060, "end_time": T0 + 0.740,
        "http": {"request": {"method": "POST", "url": "http://cotizacion:8080/cotizaciones"}},
        "error": error_en_aliado,
        "subsegments": [
            {"id": "c1-op", "name": "perfilamiento", "start_time": T0 + 0.065, "end_time": T0 + 0.735,
             "annotations": {"operation": "perfilamiento"},
             "subsegments": [{
                 "id": "c1-aliado", "name": "simulador-aliados", "namespace": "remote",
                 "start_time": T0 + 0.100, "end_time": T0 + 0.500, "fault": error_en_aliado,
             }]},
        ],
    }
    return [socios, cotizacion]


# --- Dominio ---------------------------------------------------------------------------
def test_desglose_por_servicio() -> None:
    detalle = construir_detalle([TRACE_ID], documentos(), umbral_ms=1500)
    assert detalle is not None
    filas = [(t.servicio, t.operacion, t.externo, t.inicio_ms, t.duracion_ms, t.propio_ms) for t in detalle.tramos]
    assert filas == [
        ("api-socios", "solicitar_cotizacion", False, 0, 800, 120),
        ("cotizacion", "perfilamiento", False, 60, 680, 280),
        ("simulador-aliados", "externo", True, 100, 400, 400),
    ]
    assert detalle.duracion_total_ms == 800
    assert detalle.correlation_id == "prueba-123"
    assert detalle.servicio_entrada == "api-socios"
    assert (detalle.servicios_internos, detalle.servicios_externos) == (2, 1)
    assert detalle.tramos[0].participacion == 1.0 and detalle.tramos[2].participacion == 0.5
    assert detalle.contexto["canal"] == "Socios"
    assert detalle.inicio == datetime.fromtimestamp(T0, UTC)


def test_interpretacion_destaca_los_dos_tramos_mas_costosos() -> None:
    detalle = construir_detalle([TRACE_ID], documentos(), umbral_ms=1500)
    assert detalle.interpretacion == (
        "El paso más costoso es simulador-aliados · externo con 400 ms, seguido por cotizacion · perfilamiento "
        "con 280 ms. Entre los dos consumen el 85 % del recorrido. La duración total está dentro del límite de 1,50 s."
    )
    assert [t.servicio for t in detalle.tramos if t.destacado] == ["cotizacion", "simulador-aliados"]


def test_interpretacion_sobre_el_umbral_y_con_errores() -> None:
    detalle = construir_detalle([TRACE_ID], documentos(error_en_aliado=True), umbral_ms=500)
    assert detalle.resultado == ESTADO_ERROR
    assert "supera el límite de 0,50 s: estos dos tramos son los candidatos a optimizar." in detalle.interpretacion
    assert "Terminaron con error: cotizacion · perfilamiento, simulador-aliados · externo." in detalle.interpretacion


def test_un_solo_tramo_y_sin_tramos() -> None:
    solo = [{"id": "x", "name": "bff-web", "start_time": T0, "end_time": T0 + 0.01,
             "http": {"request": {"method": "GET", "url": "http://bff-web:8080/health?x=1"}}}]
    detalle = construir_detalle(["t"], solo, umbral_ms=1500)
    assert detalle.tramos[0].operacion == "GET /health"
    assert detalle.interpretacion.startswith("El único paso registrado es bff-web · GET /health con 10 ms.")
    assert detalle.resultado == ESTADO_CORRECTO
    assert construir_detalle(["t"], [], umbral_ms=1500) is None
    assert interpretar([], 1, 1) == "La traza no tiene tramos registrados."


def test_formatos_es_co() -> None:
    assert formatear_ms(1026) == "1.026 ms"
    assert formatear_segundos(1280) == "1,28 s"


# --- Adaptador X-Ray ----------------------------------------------------------------------
class XRayFalso:
    def __init__(self, paginas: list[dict[str, Any]], trazas: dict[str, list[dict[str, Any]]]) -> None:
        self.paginas = paginas
        self.trazas = trazas
        self.llamadas: list[dict[str, Any]] = []

    def get_trace_summaries(self, **kwargs: Any) -> dict[str, Any]:
        self.llamadas.append(kwargs)
        return self.paginas.pop(0) if self.paginas else {"TraceSummaries": []}

    def batch_get_traces(self, TraceIds: list[str], **_: Any) -> dict[str, Any]:
        return {"Traces": [
            {"Id": i, "Segments": [{"Id": d["id"], "Document": json.dumps(d)} for d in self.trazas.get(i, [])]}
            for i in TraceIds
        ]}


AHORA = datetime(2026, 10, 8, 12, tzinfo=UTC)


def test_expresion_de_filtro() -> None:
    filtro = FiltroTrazas(correlation_id="prueba-123", entrada="api-socios", duracion_min_ms=1500)
    assert expresion_filtro(filtro) == (
        'annotation.correlation_id = "prueba-123" AND service("api-socios") AND duration >= 1.500'
    )
    assert expresion_filtro(FiltroTrazas()) is None


@pytest.mark.parametrize("filtro", [
    FiltroTrazas(correlation_id='x" OR annotation.a = "b'),
    FiltroTrazas(entrada='api-socios") OR service("x'),
])
def test_expresion_rechaza_inyeccion(filtro: FiltroTrazas) -> None:
    with pytest.raises(ValueError):
        expresion_filtro(filtro)


def test_busca_por_ventanas_de_6_horas_y_pagina() -> None:
    xray = XRayFalso(
        paginas=[
            {"TraceSummaries": [], "NextToken": None},  # 06:00-12:00
            {"TraceSummaries": [{"Id": "a", "StartTime": AHORA - timedelta(hours=8)}], "NextToken": "p2"},
            {"TraceSummaries": [{"Id": "b", "StartTime": AHORA - timedelta(hours=7)}]},
        ],
        trazas={},
    )
    repo = RepositorioXRay(xray, ahora=lambda: AHORA)
    assert repo.buscar(FiltroTrazas(correlation_id="prueba-123", horas=24), limite=5) == ["b", "a"]
    assert xray.llamadas[0]["StartTime"] == AHORA - timedelta(hours=6)
    assert xray.llamadas[0]["FilterExpression"] == 'annotation.correlation_id = "prueba-123"'
    assert xray.llamadas[2]["NextToken"] == "p2"
    # 4 ventanas para 24 h; la segunda tiene 2 páginas.
    assert len(xray.llamadas) == 5
    assert xray.llamadas[-1]["StartTime"] == AHORA - timedelta(hours=24)


def test_deja_de_buscar_al_llenar_el_limite() -> None:
    xray = XRayFalso(paginas=[{"TraceSummaries": [{"Id": "a"}]}], trazas={})
    assert RepositorioXRay(xray, ahora=lambda: AHORA).buscar(FiltroTrazas(horas=24), limite=1) == ["a"]
    assert len(xray.llamadas) == 1


def test_filtra_por_servicio_de_entrada() -> None:
    xray = XRayFalso(paginas=[{"TraceSummaries": [
        {"Id": "por-web", "EntryPoint": {"Name": "bff-web"}},
        {"Id": "por-socios", "EntryPoint": {"Name": "api-socios"}},
    ]}], trazas={})
    repo = RepositorioXRay(xray, ahora=lambda: AHORA)
    assert repo.buscar(FiltroTrazas(entrada="api-socios", horas=6), limite=1) == ["por-socios"]


def test_documentos_por_lotes_de_5() -> None:
    xray = XRayFalso(paginas=[], trazas={f"t{i}": [{"id": f"s{i}", "name": "x", "start_time": T0}] for i in range(7)})
    docs = RepositorioXRay(xray).documentos([f"t{i}" for i in range(7)])
    assert [d["id"] for d in docs] == [f"s{i}" for i in range(7)]


# --- Ruta HTTP ------------------------------------------------------------------------------
class RepositorioEnMemoria:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.filtros: list[FiltroTrazas] = []

    def buscar(self, filtro: FiltroTrazas, limite: int) -> list[str]:
        self.filtros.append(filtro)
        return [TRACE_ID] if self.docs else []

    def documentos(self, trace_ids: list[str]) -> list[dict[str, Any]]:
        return self.docs


OPERACION = {"X-Authenticated-Groups": "[operacion]"}


@pytest.fixture
def cliente() -> Any:
    repo = RepositorioEnMemoria(documentos())
    app.dependency_overrides[ruta.consultar_traza] = lambda: ConsultarTraza(repo, umbral_ms=1500)
    yield TestClient(app), repo
    app.dependency_overrides.clear()


def test_traza_por_identificador(cliente: Any) -> None:
    http, repo = cliente
    respuesta = http.get("/operacion/trazas", params={"correlation_id": "prueba-123", "recorrido": "api-socios",
                                                      "duracion_min_ms": 100}, headers=OPERACION)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["correlation_id"] == "prueba-123"
    assert cuerpo["duracion_total_ms"] == 800 and cuerpo["umbral_ms"] == 1500
    assert [t["servicio"] for t in cuerpo["tramos"]] == ["api-socios", "cotizacion", "simulador-aliados"]
    assert cuerpo["contexto"] == {"canal": "Socios", "servicio_entrada": "api-socios", "servicios_internos": 2,
                                  "servicios_externos": 1, "ambiente": "local"}
    assert repo.filtros == [FiltroTrazas("prueba-123", "api-socios", 100, 6)]


@pytest.mark.parametrize("encabezados", [{}, {"X-Authenticated-Groups": "[administradores-socios]"},
                                          {"X-Authenticated-Groups": "operaciones"}])
def test_solo_el_grupo_operacion(cliente: Any, encabezados: dict[str, str]) -> None:
    http, _ = cliente
    assert http.get("/operacion/trazas", headers=encabezados).status_code == 403


def test_emisor_debe_ser_el_back_office(cliente: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    http, _ = cliente
    monkeypatch.setitem(ruta.settings.jwt_issuers, "backoffice", "https://cognito-idp/backoffice")
    otro = {**OPERACION, "X-Authenticated-Issuer": "https://cognito-idp/clientes"}
    assert http.get("/operacion/trazas", headers=otro).status_code == 403
    propio = {"X-Authenticated-Groups": "operacion,administradores-socios", "X-Authenticated-Issuer": "https://cognito-idp/backoffice"}
    assert http.get("/operacion/trazas", headers=propio).status_code == 200


@pytest.mark.parametrize("params", [{"correlation_id": "con espacio"}, {"correlation_id": "x" * 129},
                                    {"recorrido": "catalogo"}, {"duracion_min_ms": -1}, {"horas": 48}])
def test_parametros_invalidos(cliente: Any, params: dict[str, Any]) -> None:
    http, _ = cliente
    assert http.get("/operacion/trazas", params=params, headers=OPERACION).status_code in (400, 422)


def test_sin_resultados_404() -> None:
    app.dependency_overrides[ruta.consultar_traza] = lambda: ConsultarTraza(RepositorioEnMemoria([]), umbral_ms=1500)
    try:
        respuesta = TestClient(app).get("/operacion/trazas", params={"correlation_id": "nada"}, headers=OPERACION)
    finally:
        app.dependency_overrides.clear()
    assert respuesta.status_code == 404


def test_cors_para_el_portal(cliente: Any) -> None:
    http, _ = cliente
    respuesta = http.options("/operacion/trazas", headers={
        "Origin": "http://localhost:4200", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization,x-correlation-id",
    })
    assert respuesta.status_code == 200
    assert respuesta.headers["access-control-allow-origin"] == "http://localhost:4200"
    get = http.get("/operacion/trazas", headers={**OPERACION, "Origin": "http://localhost:4200"})
    assert "X-Correlation-Id" in get.headers["access-control-expose-headers"]
