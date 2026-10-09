"""Arnés de módulo: WireMock 3.9.2 real con los mapeos versionados (03, W10-P08; lo reutiliza W12-P05)."""

import io
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from adaptador_datos.config import Settings
from adaptador_datos.main import crear_app
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from solventa_common.logging import FormateadorJson
from testcontainers.core.container import DockerContainer

SIMULADOR = Path(__file__).resolve().parents[3] / "simulador-aliados"
IMAGEN = "wiremock/wiremock:3.9.2"

# Valores que nunca pueden aparecer en logs ni trazas (01 §3.9 y 03 §B.9).
CENTINELAS = (
    "1800000",
    "6500000",
    "180000000",
    "1990-04-18",
    "local-sintetico",
    "SIN-CTA-0004",
    "Cliente Sintético 04",
)


class WireMock:
    def __init__(self, url: str) -> None:
        self.url = url
        self._admin = httpx.Client(base_url=f"{url}/__admin", timeout=5)

    def solicitudes(self) -> list[dict[str, Any]]:
        """Journal, de la más antigua a la más reciente."""
        return [r["request"] for r in reversed(self._admin.get("/requests").json()["requests"])]

    def de_cliente(self, cliente_id: str) -> list[dict[str, Any]]:
        return [r for r in self.solicitudes() if cliente_id in r["url"]]

    def limpiar_journal(self) -> None:
        self._admin.delete("/requests").raise_for_status()

    def reiniciar_escenarios(self) -> None:
        self._admin.post("/scenarios/reset").raise_for_status()

    def esperar_listo(self, segundos: float = 60) -> None:
        limite = time.monotonic() + segundos
        while time.monotonic() < limite:
            try:
                if self._admin.get("/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError("WireMock no arrancó")


def encabezado(solicitud: dict[str, Any], nombre: str) -> str | None:
    for clave, valor in solicitud["headers"].items():
        if clave.lower() == nombre.lower():
            return valor
    return None


@pytest.fixture(scope="session")
def wiremock() -> Iterator[WireMock]:
    contenedor = (
        DockerContainer(IMAGEN)
        .with_exposed_ports(8080)
        .with_volume_mapping(str(SIMULADOR / "mappings"), "/home/wiremock/mappings", "ro")
        .with_volume_mapping(str(SIMULADOR / "__files"), "/home/wiremock/__files", "ro")
        .with_command("--disable-banner")
    )
    with contenedor:
        servidor = WireMock(
            f"http://{contenedor.get_container_host_ip()}:{contenedor.get_exposed_port(8080)}"
        )
        servidor.esperar_listo()
        yield servidor


@dataclass
class Registro:
    """Logs y spans de toda la suite, para la prueba de centinelas."""

    logs: list[io.StringIO] = field(default_factory=list)
    spans: list[InMemorySpanExporter] = field(default_factory=list)

    def texto(self) -> str:
        partes = [salida.getvalue() for salida in self.logs]
        for exportador in self.spans:
            for span in exportador.get_finished_spans():
                partes.append(f"{span.name} {dict(span.attributes or {})}")
                partes.extend(str(dict(e.attributes or {})) for e in span.events)
        return "\n".join(partes)


@pytest.fixture(scope="session")
def centinelas() -> tuple[str, ...]:
    return CENTINELAS


@pytest.fixture(scope="session")
def leer_encabezado() -> Callable[[dict[str, Any], str], str | None]:
    return encabezado


@pytest.fixture(scope="session")
def registro() -> Registro:
    return Registro()


@dataclass
class Adaptador:
    cliente: TestClient
    logs: io.StringIO
    spans: InMemorySpanExporter

    def texto_spans(self) -> str:
        return " ".join(
            f"{s.name} {dict(s.attributes or {})}" for s in self.spans.get_finished_spans()
        )


@pytest.fixture
def app_adaptador(
    wiremock: WireMock, registro: Registro, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Adaptador]]:
    """Fábrica: una app nueva por prueba, apuntada al WireMock; **env sobrescribe variables."""
    wiremock.limpiar_journal()
    wiremock.reiniciar_escenarios()
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level

    abiertos: list[TestClient] = []

    def crear(**env: str) -> Adaptador:
        variables = {
            "ALLY_OPEN_FINANCE_URL": f"{wiremock.url}/open-finance",
            "ALLY_DATOS_ABIERTOS_URL": f"{wiremock.url}/datos-abiertos",
            "OTEL_SDK_DISABLED": "false",
            **env,
        }
        for clave, valor in variables.items():
            monkeypatch.setenv(clave, valor)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        spans = InMemorySpanExporter()
        app = crear_app(Settings(), span_exporter=spans, metric_readers=[InMemoryMetricReader()])
        salida = io.StringIO()
        handler = logging.StreamHandler(salida)
        handler.setFormatter(FormateadorJson("adaptador-datos", "pruebas"))
        raiz.handlers = [handler]
        raiz.setLevel(logging.INFO)
        cliente = TestClient(app)
        cliente.__enter__()
        abiertos.append(cliente)
        registro.logs.append(salida)
        registro.spans.append(spans)
        return Adaptador(cliente=cliente, logs=salida, spans=spans)

    yield crear
    for cliente in abiertos:
        cliente.__exit__(None, None, None)
    raiz.handlers, raiz.level = anteriores, nivel
