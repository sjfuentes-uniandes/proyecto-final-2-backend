import io
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from solventa_common import telemetry
from solventa_common.logging import FormateadorJson
from solventa_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "servicio-prueba"


@dataclass
class Telemetria:
    spans: InMemorySpanExporter
    lector: InMemoryMetricReader
    salida: io.StringIO

    def logs(self) -> list[dict[str, Any]]:
        return [json.loads(linea) for linea in self.salida.getvalue().splitlines() if linea.strip()]

    def texto_logs(self) -> str:
        return self.salida.getvalue()

    def lista_spans(self) -> list[ReadableSpan]:
        return list(self.spans.get_finished_spans())

    def puntos(self, nombre: str) -> list[Any]:
        datos = self.lector.get_metrics_data()
        if datos is None:
            return []
        return [
            punto
            for recurso in datos.resource_metrics
            for alcance in recurso.scope_metrics
            for metrica in alcance.metrics
            if metrica.name == nombre
            for punto in metrica.data.data_points
        ]

    def recurso(self) -> dict[str, Any]:
        datos = self.lector.get_metrics_data()
        assert datos is not None
        return dict(datos.resource_metrics[0].resource.attributes)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ENVIRONMENT", "pruebas")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    return Settings()


@pytest.fixture
def capturar_logs() -> Iterator[Callable[[str, str], io.StringIO]]:
    """Redirige el logger raíz a un buffer con el formateador JSON."""
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level

    def capturar(servicio: str = "servicio-prueba", ambiente: str = "pruebas") -> io.StringIO:
        salida = io.StringIO()
        handler = logging.StreamHandler(salida)
        handler.setFormatter(FormateadorJson(servicio, ambiente))
        raiz.handlers = [handler]
        raiz.setLevel(logging.INFO)
        return salida

    yield capturar
    raiz.handlers, raiz.level = anteriores, nivel


@pytest.fixture
def telemetria(settings: Settings, capturar_logs: Callable[..., io.StringIO]) -> Telemetria:
    spans = InMemorySpanExporter()
    lector = InMemoryMetricReader()
    telemetry.configurar(settings, span_exporter=spans, metric_readers=[lector])
    return Telemetria(spans=spans, lector=lector, salida=capturar_logs(settings.service_name, settings.environment))
