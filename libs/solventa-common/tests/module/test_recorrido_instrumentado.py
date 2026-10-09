"""N2 (HU-W27): dos servicios instrumentados con exportadores en memoria.

`api-socios` recibe la solicitud y llama a `cotizacion` con httpx. El segundo
servicio se atiende en un contexto vacío (contextvars.Context()), como si fuera
otro proceso: el correlationId y la traza solo llegan si viajan en los encabezados.
"""

import contextvars
import io
import json
import logging
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from solventa_common import correlation, telemetry
from solventa_common.logging import trace_id_xray
from solventa_common.settings import ServiceSettings
from solventa_common.telemetry import instrumentar, operacion

SECRETO = "cl1ent-s3cret-no-loguear"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzY29wZSI6InNvY2lvcyJ9.firma"


class SettingsSocios(ServiceSettings):
    service_name: str = "api-socios"


class SettingsCotizacion(ServiceSettings):
    service_name: str = "cotizacion"


@dataclass
class Recorrido:
    socios: TestClient
    spans_socios: InMemorySpanExporter
    spans_cotizacion: InMemorySpanExporter
    lector: InMemoryMetricReader
    logs: io.StringIO
    recibido_por_cotizacion: list[dict[str, Any]]

    def lineas(self) -> list[dict[str, Any]]:
        return [json.loads(linea) for linea in self.logs.getvalue().splitlines() if linea.strip()]


class TransporteAislado(httpx.BaseTransport):
    """Ejecuta el otro servicio sin heredar el contexto (correlación ni span) del llamador."""

    def __init__(self, interno: httpx.BaseTransport) -> None:
        self.interno = interno

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return contextvars.Context().run(self.interno.handle_request, request)


def _app_cotizacion(recibidos: list[dict[str, Any]]) -> FastAPI:
    app = FastAPI()

    @app.post("/cotizaciones")
    async def cotizar(datos: dict[str, Any]) -> dict[str, Any]:
        with operacion("cotizar"):
            recibidos.append({"correlation_id": correlation.obtener()})
            return {"cotizacion": "COT-1", "prima": 120_000}

    return app


def _app_socios(transporte_cotizacion: httpx.BaseTransport, con_propagacion: bool) -> FastAPI:
    app = FastAPI()

    @app.post("/socios/cotizaciones")
    async def solicitar(datos: dict[str, Any]) -> dict[str, Any]:
        with operacion("solicitar_cotizacion"):
            logging.getLogger("api_socios").info(
                "solicitud recibida",
                extra={"socio": "SOC-000114", "credenciales": {"client_secret": SECRETO}, "payload": datos},
            )
            fabrica = correlation.cliente if con_propagacion else httpx.Client
            with fabrica(transport=transporte_cotizacion, base_url="http://cotizacion:8080") as cliente:
                # instrumentar() parchea httpx.HTTPTransport, el transporte real entre servicios;
                # aquí el transporte es el TestClient de cotización y se instrumenta el cliente.
                HTTPXClientInstrumentor.instrument_client(cliente, tracer_provider=telemetry._estado.tracer_provider)
                respuesta = cliente.post("/cotizaciones", json={"producto": "vida"})
            return respuesta.json()

    return app


@pytest.fixture
def recorrido(monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[..., io.StringIO]) -> Iterator[Callable[..., Recorrido]]:
    monkeypatch.setenv("ENVIRONMENT", "int")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    def crear(con_propagacion: bool = True) -> Recorrido:
        recibidos: list[dict[str, Any]] = []
        spans_cotizacion = InMemorySpanExporter()
        cotizacion = instrumentar(_app_cotizacion(recibidos), SettingsCotizacion(), span_exporter=spans_cotizacion)
        transporte = TransporteAislado(TestClient(cotizacion)._transport)

        spans_socios, lector = InMemorySpanExporter(), InMemoryMetricReader()
        socios = instrumentar(
            _app_socios(transporte, con_propagacion),
            SettingsSocios(),
            span_exporter=spans_socios,
            metric_readers=[lector],
        )
        logs = capturar_logs("api-socios", "int")
        return Recorrido(TestClient(socios), spans_socios, spans_cotizacion, lector, logs, recibidos)

    yield crear


def _servidor(spans: tuple[ReadableSpan, ...]) -> ReadableSpan:
    return next(s for s in spans if s.kind == SpanKind.SERVER)


def _puntos(lector: InMemoryMetricReader, metrica: str, operacion_: str) -> list[Any]:
    datos = lector.get_metrics_data()
    return [
        (punto, dict(r.resource.attributes))
        for r in datos.resource_metrics
        for s in r.scope_metrics
        for m in s.metrics
        if m.name == metrica
        for punto in m.data.data_points
        if punto.attributes.get("Operation") == operacion_
    ]


def test_log_metrica_y_traza_comparten_operacion_servicio_ambiente_y_correlacion(recorrido) -> None:
    r = recorrido()
    respuesta = r.socios.post(
        "/socios/cotizaciones",
        json={"ingresos": 9_500_000, "cuenta": "001-998877"},
        headers={"X-Correlation-Id": "prueba-123", "Authorization": f"Bearer {TOKEN}"},
    )
    assert respuesta.status_code == 200
    assert respuesta.headers["X-Correlation-Id"] == "prueba-123"  # AC2

    # Log de la operación.
    log = next(linea for linea in r.lineas() if linea["operation"] == "solicitar_cotizacion" and linea["result"])
    assert (log["service"], log["environment"], log["correlation_id"], log["result"]) == (
        "api-socios", "int", "prueba-123", "ok",
    )
    assert log["duration_ms"] > 0

    # Métrica de la misma operación con las dimensiones del colector.
    [(punto, recurso)] = _puntos(r.lector, "OperationDuration", "solicitar_cotizacion")
    assert punto.count == 1
    assert (recurso["Service"], recurso["Environment"]) == ("api-socios", "int")
    [(errores, _)] = _puntos(r.lector, "OperationErrors", "solicitar_cotizacion")
    assert errores.value == 0

    # Traza: span de la operación con el mismo correlationId y el trace_id del log.
    spans = r.spans_socios.get_finished_spans()
    span = next(s for s in spans if s.name == "solicitar_cotizacion")
    assert span.attributes["correlation_id"] == "prueba-123"
    assert span.attributes["operation"] == "solicitar_cotizacion"
    assert span.resource.attributes["Service"] == "api-socios"
    assert span.resource.attributes["Environment"] == "int"
    assert trace_id_xray(span.context.trace_id) == log["trace_id"]


def test_httpx_conserva_el_id_y_la_traza_en_el_segundo_servicio(recorrido) -> None:
    r = recorrido()
    respuesta = r.socios.post("/socios/cotizaciones", json={}, headers={"X-Correlation-Id": "prueba-123"})
    assert respuesta.json()["cotizacion"] == "COT-1"
    assert r.recibido_por_cotizacion == [{"correlation_id": "prueba-123"}]

    servidor_socios = _servidor(r.spans_socios.get_finished_spans())
    servidor_cotizacion = _servidor(r.spans_cotizacion.get_finished_spans())
    cliente_httpx = next(s for s in r.spans_socios.get_finished_spans() if s.kind == SpanKind.CLIENT)
    # Misma traza en ambos componentes y el servidor remoto cuelga del span del cliente httpx.
    assert servidor_cotizacion.context.trace_id == servidor_socios.context.trace_id
    assert servidor_cotizacion.parent.span_id == cliente_httpx.context.span_id
    assert {s.attributes["correlation_id"] for s in r.spans_cotizacion.get_finished_spans()} == {"prueba-123"}
    assert cliente_httpx.attributes["correlation_id"] == "prueba-123"


def test_sin_id_la_entrada_lo_genera_y_todo_el_recorrido_lo_usa(recorrido) -> None:
    r = recorrido()
    respuesta = r.socios.post("/socios/cotizaciones", json={})
    generado = respuesta.headers["X-Correlation-Id"]
    assert uuid.UUID(generado).version == 4  # AC1
    assert r.recibido_por_cotizacion == [{"correlation_id": generado}]
    todos = (*r.spans_socios.get_finished_spans(), *r.spans_cotizacion.get_finished_spans())
    assert {s.attributes["correlation_id"] for s in todos} == {generado}
    assert {linea["correlation_id"] for linea in r.lineas()} == {generado}


def test_id_de_api_gateway_cuando_el_socio_no_envia_uno(recorrido) -> None:
    r = recorrido()
    respuesta = r.socios.post("/socios/cotizaciones", json={}, headers={"X-Request-Id": "c6af9ac6-7b61-11e6-9a41-93e8deadbeef"})
    assert respuesta.headers["X-Correlation-Id"] == "c6af9ac6-7b61-11e6-9a41-93e8deadbeef"
    assert r.recibido_por_cotizacion == [{"correlation_id": "c6af9ac6-7b61-11e6-9a41-93e8deadbeef"}]


def test_control_sin_hook_el_segundo_servicio_no_recibe_el_id(recorrido) -> None:
    # Demuestra que la prueba anterior no pasa por contexto compartido entre servicios.
    r = recorrido(con_propagacion=False)
    r.socios.post("/socios/cotizaciones", json={}, headers={"X-Correlation-Id": "prueba-123"})
    assert r.recibido_por_cotizacion[0]["correlation_id"] != "prueba-123"


def test_seguridad_logs_y_trazas_sin_secretos_ni_payload_financiero(recorrido) -> None:
    r = recorrido()
    r.socios.post(
        "/socios/cotizaciones",
        json={"ingresos": 9_500_000, "obligaciones": [{"saldo": 4_400_000}], "selfie": "iVBORw0KGgo" * 50},
        headers={"X-Correlation-Id": "prueba-123", "Authorization": f"Bearer {TOKEN}", "x-api-key": SECRETO},
    )
    texto_logs = r.logs.getvalue()
    texto_spans = json.dumps(
        [{"name": s.name, "attributes": dict(s.attributes), "events": [dict(e.attributes or {}) for e in s.events]}
         for s in (*r.spans_socios.get_finished_spans(), *r.spans_cotizacion.get_finished_spans())],
        default=str,
    )
    for prohibido in (SECRETO, TOKEN, "9500000", "4400000", "iVBORw0KGgo"):
        assert prohibido not in texto_logs, prohibido
        assert prohibido not in texto_spans, prohibido
    payload = next(linea["payload"] for linea in r.lineas() if "payload" in linea)
    assert payload == {"ingresos": "[REDACTADO]", "obligaciones": "[REDACTADO]", "selfie": "[REDACTADO]"}
