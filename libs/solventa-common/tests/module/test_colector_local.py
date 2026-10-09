"""N2 (HU-W27) con colector local: el servicio exporta por OTLP/gRPC como en ECS.

Un receptor OTLP en el proceso hace de colector ADOT (localhost:4317 en la
tarea). Se verifica lo que realmente sale del servicio: nombres de métrica que
filtra el colector (^Operation.*), histograma exponencial para el p95, atributos
de recurso que se vuelven dimensiones (Service, Environment) y el correlationId
en los spans, junto con el log de la misma operación.
"""

import io
import json
import threading
from collections.abc import Callable, Iterator
from concurrent import futures
from typing import Any

import grpc
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2, metrics_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
from opentelemetry.proto.common.v1.common_pb2 import KeyValue
from solventa_common import telemetry
from solventa_common.logging import trace_id_xray
from solventa_common.settings import ServiceSettings
from solventa_common.telemetry import instrumentar, operacion


class SettingsCotizacion(ServiceSettings):
    service_name: str = "cotizacion"


class ColectorLocal(trace_service_pb2_grpc.TraceServiceServicer, metrics_service_pb2_grpc.MetricsServiceServicer):
    def __init__(self) -> None:
        self.trazas: list[trace_service_pb2.ExportTraceServiceRequest] = []
        self.metricas: list[metrics_service_pb2.ExportMetricsServiceRequest] = []
        self._lock = threading.Lock()

    def Export(self, request: Any, context: Any) -> Any:  # nombre que impone el servicio gRPC
        with self._lock:
            if isinstance(request, trace_service_pb2.ExportTraceServiceRequest):
                self.trazas.append(request)
                return trace_service_pb2.ExportTraceServiceResponse()
            self.metricas.append(request)
            return metrics_service_pb2.ExportMetricsServiceResponse()


def _atributos(valores: Any) -> dict[str, Any]:
    def valor(kv: KeyValue) -> Any:
        campo = kv.value.WhichOneof("value")
        return getattr(kv.value, campo) if campo else None

    return {kv.key: valor(kv) for kv in valores}


@pytest.fixture
def colector() -> Iterator[tuple[ColectorLocal, int]]:
    receptor = ColectorLocal()
    servidor = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(receptor, servidor)
    metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(receptor, servidor)
    puerto = servidor.add_insecure_port("127.0.0.1:0")
    servidor.start()
    yield receptor, puerto
    servidor.stop(None)


@pytest.fixture
def servicio(
    monkeypatch: pytest.MonkeyPatch, colector: tuple[ColectorLocal, int], capturar_logs: Callable[..., io.StringIO]
) -> Iterator[tuple[TestClient, ColectorLocal, io.StringIO]]:
    receptor, puerto = colector
    # Contrato de infra/apps/ecs.tf, apuntando al colector local.
    monkeypatch.setenv("ENVIRONMENT", "int")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{puerto}")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION", "base2_exponential_bucket_histogram")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)

    app = FastAPI()

    @app.post("/perfilamiento")
    async def perfilar(datos: dict[str, Any]) -> dict[str, str]:
        with operacion("perfilamiento"):
            return {"segmento": "B"}

    instrumentar(app, SettingsCotizacion())
    logs = capturar_logs("cotizacion", "int")
    yield TestClient(app), receptor, logs
    telemetry.apagar()


def _exportar() -> None:
    assert telemetry._estado.tracer_provider.force_flush(10_000)
    assert telemetry._estado.meter_provider.force_flush(10_000)


def test_log_metrica_y_traza_llegan_al_colector_con_la_misma_correlacion(servicio) -> None:
    cliente, receptor, logs = servicio
    respuesta = cliente.post(
        "/perfilamiento", json={"ingresos": 9_500_000}, headers={"X-Correlation-Id": "prueba-123"}
    )
    assert respuesta.headers["X-Correlation-Id"] == "prueba-123"
    _exportar()

    # Traza recibida por OTLP: span de la operación con recurso y correlación.
    spans = [
        (_atributos(rs.resource.attributes), span)
        for envio in receptor.trazas
        for rs in envio.resource_spans
        for ss in rs.scope_spans
        for span in ss.spans
    ]
    recurso, span = next((r, s) for r, s in spans if s.name == "perfilamiento")
    assert (recurso["Service"], recurso["Environment"], recurso["service.name"]) == ("cotizacion", "int", "cotizacion")
    assert _atributos(span.attributes)["correlation_id"] == "prueba-123"
    assert _atributos(span.attributes)["operation"] == "perfilamiento"
    assert {_atributos(s.attributes).get("correlation_id") for _, s in spans} == {"prueba-123"}

    # Métricas recibidas: nombres exactos, atributo Operation e histograma exponencial (p95).
    metricas = {
        metrica.name: (metrica, _atributos(rm.resource.attributes))
        for envio in receptor.metricas
        for rm in envio.resource_metrics
        for sm in rm.scope_metrics
        for metrica in sm.metrics
    }
    assert {"OperationDuration", "OperationErrors"} <= set(metricas)
    duracion, recurso_metrica = metricas["OperationDuration"]
    assert duracion.unit == "ms"
    assert duracion.WhichOneof("data") == "exponential_histogram"
    operaciones = {_atributos(p.attributes)["Operation"] for p in duracion.exponential_histogram.data_points}
    assert {"perfilamiento", "POST /perfilamiento"} <= operaciones
    assert (recurso_metrica["Service"], recurso_metrica["Environment"]) == ("cotizacion", "int")

    # Log de la misma operación: servicio, ambiente, correlación y trace_id de la traza exportada.
    log = next(json.loads(linea) for linea in logs.getvalue().splitlines() if '"operation": "perfilamiento"' in linea)
    assert (log["service"], log["environment"], log["correlation_id"], log["result"]) == (
        "cotizacion", "int", "prueba-123", "ok",
    )
    assert log["trace_id"] == trace_id_xray(int.from_bytes(span.trace_id, "big"))

    # Nada sensible sale hacia el colector.
    exportado = "".join(str(e) for e in receptor.trazas)
    assert "9500000" not in exportado and "ingresos" not in exportado


def test_errores_llegan_como_operation_errors(servicio) -> None:
    cliente, receptor, _ = servicio
    cliente.post("/no-existe", json={})
    _exportar()
    errores = [
        (_atributos(p.attributes)["Operation"], p.as_int)
        for envio in receptor.metricas
        for rm in envio.resource_metrics
        for sm in rm.scope_metrics
        for metrica in sm.metrics
        if metrica.name == "OperationErrors"
        for p in metrica.sum.data_points
    ]
    # 404 = rechazado: cuenta en volumen pero no como error.
    assert ("POST sin_ruta", 0) in errores
