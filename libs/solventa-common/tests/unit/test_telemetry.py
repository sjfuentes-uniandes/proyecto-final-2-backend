import asyncio
import logging
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from solventa_common import correlation, telemetry
from solventa_common.telemetry import (
    METRICA_DURACION,
    METRICA_ERRORES,
    RESULTADO_RECHAZADO,
    instrumentar,
    operacion,
)

Telemetria = Any
Settings = Any


def _por_operacion(puntos: list, nombre: str) -> list:
    return [p for p in puntos if p.attributes.get("Operation") == nombre]


def test_exito_registra_duracion_error_cero_span_y_log(telemetria: Telemetria) -> None:
    with operacion("perfilamiento"):
        pass
    duracion = _por_operacion(telemetria.puntos(METRICA_DURACION), "perfilamiento")
    errores = _por_operacion(telemetria.puntos(METRICA_ERRORES), "perfilamiento")
    assert duracion[0].count == 1 and duracion[0].sum >= 0
    assert dict(duracion[0].attributes) == {"Operation": "perfilamiento"}
    assert errores[0].value == 0
    span = telemetria.lista_spans()[0]
    assert span.name == "perfilamiento" and span.attributes["result"] == "ok"
    log = telemetria.logs()[-1]
    assert (log["operation"], log["result"]) == ("perfilamiento", "ok")
    assert isinstance(log["duration_ms"], float)


def test_error_cuenta_y_propaga_la_excepcion(telemetria: Telemetria) -> None:
    with pytest.raises(ValueError), operacion("emitir"):
        raise ValueError("ingresos=9500000 del cliente")
    assert _por_operacion(telemetria.puntos(METRICA_ERRORES), "emitir")[0].value == 1
    assert _por_operacion(telemetria.puntos(METRICA_DURACION), "emitir")[0].count == 1
    span = telemetria.lista_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "ValueError"
    log = telemetria.logs()[-1]
    assert (log["level"], log["result"], log["error_type"]) == ("ERROR", "error", "ValueError")
    # El mensaje de la excepción (con datos del cliente) no llega a logs ni trazas.
    assert "9500000" not in telemetria.texto_logs()
    assert all("9500000" not in str(e.attributes) for e in span.events)


@pytest.mark.parametrize("excepcion", [TimeoutError(), httpx.ReadTimeout("lento"), httpx.ConnectTimeout("sin conexión")])
def test_timeout_se_registra_como_timeout(telemetria: Telemetria, excepcion: Exception) -> None:
    with pytest.raises(type(excepcion)), operacion("consultar_aliado"):
        raise excepcion
    assert _por_operacion(telemetria.puntos(METRICA_ERRORES), "consultar_aliado")[0].value == 1
    assert telemetria.logs()[-1]["result"] == "timeout"


def test_decorador_sync_y_async(telemetria: Telemetria) -> None:
    @operacion("sumar")
    def sumar(a: int, b: int) -> int:
        return a + b

    @operacion("esperar")
    async def esperar() -> str:
        await asyncio.sleep(0)
        return "listo"

    assert sumar(1, 2) == 3
    assert asyncio.run(esperar()) == "listo"
    nombres = {p.attributes["Operation"] for p in telemetria.puntos(METRICA_DURACION)}
    assert {"sumar", "esperar"} <= nombres
    assert sumar.__name__ == "sumar"


def test_async_with_y_resultado_marcado(telemetria: Telemetria) -> None:
    async def validar() -> None:
        async with operacion("validar_scope") as op:
            op.marcar(RESULTADO_RECHAZADO)

    asyncio.run(validar())
    assert telemetria.logs()[-1]["result"] == RESULTADO_RECHAZADO
    assert _por_operacion(telemetria.puntos(METRICA_ERRORES), "validar_scope")[0].value == 0


def test_logs_internos_heredan_la_operacion(telemetria: Telemetria) -> None:
    with operacion("cotizar"):
        logging.getLogger("dominio").info("calculando prima")
    interno = next(linea for linea in telemetria.logs() if linea["message"] == "calculando prima")
    assert interno["operation"] == "cotizar"
    assert interno["trace_id"] is not None


def test_spans_llevan_el_correlation_id(telemetria: Telemetria) -> None:
    token = correlation.establecer("prueba-123")
    try:
        with operacion("padre"), operacion("hija"):
            pass
    finally:
        correlation.restablecer(token)
    assert {s.attributes["correlation_id"] for s in telemetria.lista_spans()} == {"prueba-123"}


def test_trace_ids_compatibles_con_xray(telemetria: Telemetria) -> None:
    import time

    with operacion("x"):
        pass
    trace_id = telemetria.lista_spans()[0].context.trace_id
    # Los primeros 32 bits son la época en segundos (requisito de X-Ray).
    assert abs((trace_id >> 96) - int(time.time())) < 60


def test_recurso_con_servicio_y_ambiente(telemetria: Telemetria) -> None:
    with operacion("x"):
        pass
    recurso = telemetria.recurso()
    assert recurso["Service"] == "servicio-prueba" and recurso["Environment"] == "pruebas"


def test_sdk_deshabilitado_funciona_sin_exportar(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    telemetry.configurar(settings)
    with operacion("sin_exportar"):
        pass
    app = instrumentar(FastAPI(), settings)

    @app.get("/eco")
    async def eco() -> dict[str, str | None]:
        return {"id": correlation.obtener()}

    respuesta = TestClient(app).get("/eco", headers={"X-Correlation-Id": "prueba-123"})
    assert respuesta.json() == {"id": "prueba-123"}
    assert respuesta.headers["X-Correlation-Id"] == "prueba-123"


def test_sin_endpoint_no_exporta_pero_genera_trazas(settings: Settings, capturar_logs) -> None:
    telemetry.configurar(settings)
    salida = capturar_logs()
    with operacion("local"):
        logging.getLogger("x").info("hola")
    procesadores = telemetry._estado.tracer_provider._active_span_processor._span_processors
    assert [type(p).__name__ for p in procesadores] == ["_CorrelacionEnSpans"]
    assert telemetry._estado.meter_provider._metric_readers == []
    assert '"trace_id": "1-' in salida.getvalue()


def test_medicion_por_solicitud_y_healthcheck_local(telemetria: Telemetria, settings: Settings, capturar_logs) -> None:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/socios/{socio_id}")
    async def socio(socio_id: str) -> dict[str, str]:
        if socio_id == "falla":
            raise RuntimeError("boom")
        if socio_id == "no-existe":
            from fastapi import HTTPException

            raise HTTPException(404)
        return {"id": socio_id}

    telemetria.spans, telemetria.lector = InMemorySpanExporter(), InMemoryMetricReader()
    instrumentar(app, settings, span_exporter=telemetria.spans, metric_readers=[telemetria.lector])
    telemetria.salida = capturar_logs()  # instrumentar dejó el handler JSON en stdout

    cliente = TestClient(app, raise_server_exceptions=False)
    assert cliente.get("/socios/SOC-1").status_code == 200
    assert cliente.get("/socios/no-existe").status_code == 404
    respuesta = cliente.get("/socios/falla")
    assert respuesta.status_code == 500
    assert "X-Correlation-Id" in respuesta.headers  # también en errores no controlados
    TestClient(app, client=("127.0.0.1", 5000)).get("/health")

    puntos = _por_operacion(telemetria.puntos(METRICA_DURACION), "GET /socios/{socio_id}")
    assert puntos[0].count == 3  # una sola serie: la plantilla, no el identificador
    assert _por_operacion(telemetria.puntos(METRICA_ERRORES), "GET /socios/{socio_id}")[0].value == 1
    assert not _por_operacion(telemetria.puntos(METRICA_DURACION), "GET /health")
    resultados = [linea["result"] for linea in telemetria.logs() if linea.get("operation") == "GET /socios/{socio_id}"]
    assert sorted(resultados) == ["error", "ok", "rechazado"]
    assert not [s for s in telemetria.lista_spans() if s.attributes.get("http.target") == "/health"]


def test_nombres_exactos_para_el_colector_emf(telemetria: Telemetria) -> None:
    # El colector filtra ^Operation.* y las alarmas usan OperationDuration/OperationErrors.
    with operacion("x"):
        pass
    datos = telemetria.lector.get_metrics_data()
    nombres = {m.name for r in datos.resource_metrics for s in r.scope_metrics for m in s.metrics}
    assert nombres == {"OperationDuration", "OperationErrors"}
