"""Arnés de módulo: WireMock 3.9.2 real con los mapeos versionados del simulador.

Mismo arnés que adaptador-identidad. Sin pool de Postgres ni servidor en hilo: el bucle por
defecto de Windows no afecta a estas pruebas, así que no se fija `SelectorEventLoop`.
"""

import io
import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from adaptador_firma.config import Settings
from adaptador_firma.main import crear_app
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from solventa_common.logging import FormateadorJson
from testcontainers.core.container import DockerContainer

SIMULADOR = Path(__file__).resolve().parents[3] / "simulador-aliados"
IMAGEN = "wiremock/wiremock:3.9.2"


class WireMock:
    def __init__(self, url: str) -> None:
        self.url = url
        self._admin = httpx.Client(base_url=f"{url}/__admin", timeout=5)

    def solicitudes(self) -> list[dict[str, Any]]:
        return [r["request"] for r in reversed(self._admin.get("/requests").json()["requests"])]

    def limpiar_journal(self) -> None:
        self._admin.delete("/requests").raise_for_status()

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


class Adaptador:
    def __init__(self, cliente: TestClient, logs: io.StringIO) -> None:
        self.cliente = cliente
        self.logs = logs


@pytest.fixture
def app_adaptador(
    wiremock: WireMock, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Adaptador]]:
    """Fábrica: una app nueva por prueba, apuntada al WireMock."""
    wiremock.limpiar_journal()
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level
    abiertos: list[TestClient] = []

    def crear(**env: str) -> Adaptador:
        variables = {
            "ALLY_FIRMA_URL": f"{wiremock.url}/firma",
            "OTEL_SDK_DISABLED": "false",
            **env,
        }
        for clave, valor in variables.items():
            monkeypatch.setenv(clave, valor)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        app = crear_app(
            Settings(),
            span_exporter=InMemorySpanExporter(),
            metric_readers=[InMemoryMetricReader()],
        )
        salida = io.StringIO()
        handler = logging.StreamHandler(salida)
        handler.setFormatter(FormateadorJson("adaptador-firma", "pruebas"))
        raiz.handlers = [handler]
        raiz.setLevel(logging.INFO)
        cliente = TestClient(app)
        cliente.__enter__()
        abiertos.append(cliente)
        return Adaptador(cliente=cliente, logs=salida)

    yield crear
    for cliente in abiertos:
        cliente.__exit__(None, None, None)
    raiz.handlers, raiz.level = anteriores, nivel
