"""Arnés de módulo de cotizacion (W11-P14): lo usan W10-P12, W11-P14 y W12-P08.

- PostgreSQL 16 efímero con las bases `catalogo` y `cotizacion`, migradas con su `migrar`.
- WireMock 3.9.2 con los mapeos versionados del simulador (aliados y stub de clientes).
- Las apps reales de catalogo y adaptador-datos, servidas con uvicorn en hilos.
- cotizacion con TestClient, apuntada a todo lo anterior y sin caché del modelo.
- Centinelas: los logs y spans de toda la suite se revisan al final (test_zz_centinelas.py).
"""

import io
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from solventa_common.logging import FormateadorJson

docker = pytest.importorskip("docker")

RAIZ = Path(__file__).resolve().parents[4]
SIMULADOR = RAIZ / "services" / "simulador-aliados"
EVENTOS = RAIZ / "contracts" / "events"
IMAGEN_WIREMOCK = "wiremock/wiremock:3.9.2"

# 01 §3.9: valores que nunca pueden aparecer en logs ni trazas (incluye los extra de C04).
CENTINELAS = (
    "1800000",
    "6500000",
    "180000000",
    "1990-04-18",
    "local-sintetico",
    "SIN-CTA-0004",
    "Cliente Sintético 04",
)
DECLARADOS_C01 = {
    "fechaNacimiento": "1990-04-18",
    "plazoCreditoMeses": 180,
    "sumaAsegurada": 180000000,
}
ACTOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}


def cliente_id(nn: str) -> str:
    """Identificador sintético de 01 §3.5: 00000000-0000-4000-8000-0000000000NN."""
    return f"00000000-0000-4000-8000-0000000000{nn}"


# --- PostgreSQL --------------------------------------------------------------------------


@dataclass(frozen=True)
class Bases:
    catalogo: str
    cotizacion: str

    def filas(self, sql: str, *parametros: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.cotizacion) as conexion:
            return conexion.execute(sql, parametros).fetchall()


@pytest.fixture(scope="session")
def bases() -> Iterator[Bases]:
    # Ryuk no arranca en Docker Desktop; la CI también lo desactiva.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from catalogo.adapters.outbound.modelos_pg import migrar as migrar_catalogo
    from cotizacion.adapters.outbound.perfiles_pg import migrar as migrar_cotizacion
    from testcontainers.community.postgres import PostgresContainer

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    with PostgresContainer("postgres:16", driver=None) as contenedor:
        url = contenedor.get_connection_url()
        with psycopg.connect(url, autocommit=True) as conexion:
            conexion.execute("CREATE DATABASE catalogo")
            conexion.execute("CREATE DATABASE cotizacion")
        base = url.rsplit("/", 1)[0]
        urls = Bases(catalogo=f"{base}/catalogo", cotizacion=f"{base}/cotizacion")
        migrar_catalogo(urls.catalogo)
        migrar_cotizacion(urls.cotizacion)
        yield urls


# --- WireMock ----------------------------------------------------------------------------


class WireMock:
    def __init__(self, url: str) -> None:
        self.url = url
        self._admin = httpx.Client(base_url=f"{url}/__admin", timeout=5)

    def solicitudes(self) -> list[dict[str, Any]]:
        """Journal, de la más antigua a la más reciente."""
        return [r["request"] for r in reversed(self._admin.get("/requests").json()["requests"])]

    def a_aliados(self, cliente: str) -> list[dict[str, Any]]:
        """Solicitudes a open-finance y datos-abiertos de un cliente (sin el stub de clientes)."""
        return [
            r
            for r in self.solicitudes()
            if cliente in r["url"] and r["url"].startswith(("/open-finance", "/datos-abiertos"))
        ]

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


@pytest.fixture(scope="session")
def wiremock() -> Iterator[WireMock]:
    from testcontainers.core.container import DockerContainer

    contenedor = (
        DockerContainer(IMAGEN_WIREMOCK)
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


# --- Apps reales de catalogo y adaptador-datos en hilos ----------------------------------


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto: int = s.getsockname()[1]
        return puerto


@contextmanager
def servir(app: FastAPI) -> Iterator[str]:
    """Sirve la app con uvicorn en un hilo (con su lifespan) y devuelve su URL."""
    puerto = puerto_libre()
    servidor = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=puerto, log_config=None, access_log=False)
    )
    hilo = threading.Thread(target=servidor.run, daemon=True)
    hilo.start()
    limite = time.monotonic() + 30
    while not servidor.started:
        if time.monotonic() > limite or not hilo.is_alive():
            raise RuntimeError("el servidor no arrancó")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{puerto}"
    finally:
        servidor.should_exit = True
        hilo.join(timeout=10)


@pytest.fixture(scope="session")
def catalogo_url(bases: Bases) -> Iterator[str]:
    from catalogo.config import Settings as SettingsCatalogo
    from catalogo.main import crear_app as crear_catalogo

    with servir(crear_catalogo(SettingsCatalogo(SOLVENTA_DATABASE_URL=bases.catalogo))) as url:
        yield url


@pytest.fixture(scope="session")
def adaptador_url(wiremock: WireMock) -> Iterator[str]:
    from adaptador_datos.config import Settings as SettingsAdaptador
    from adaptador_datos.main import crear_app as crear_adaptador

    settings = SettingsAdaptador(
        ALLY_OPEN_FINANCE_URL=f"{wiremock.url}/open-finance",
        ALLY_DATOS_ABIERTOS_URL=f"{wiremock.url}/datos-abiertos",
    )
    with servir(crear_adaptador(settings)) as url:
        yield url


# --- cotizacion ---------------------------------------------------------------------------


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
def registro() -> Registro:
    return Registro()


@pytest.fixture(scope="session")
def centinelas() -> tuple[str, ...]:
    return CENTINELAS


@dataclass
class Cotizacion:
    cliente: TestClient
    logs: io.StringIO
    spans: InMemorySpanExporter
    bases: Bases
    wiremock: WireMock

    def enviar(
        self,
        nn: str,
        clave: UUID | None = None,
        correlation_id: str | None = None,
        **declarados: Any,
    ) -> httpx.Response:
        """POST /v1/perfiles con los declarados de C01 (o los cambios dados) y X-Actor-*."""
        encabezados = {**ACTOR, "Idempotency-Key": str(clave or uuid4())}
        if correlation_id:
            encabezados["X-Correlation-Id"] = correlation_id
        cuerpo = {
            "clienteId": cliente_id(nn),
            "productoId": declarados.pop("productoId", "vida-hipotecario"),
            "datosDeclarados": {**DECLARADOS_C01, **declarados},
        }
        return self.cliente.post("/v1/perfiles", json=cuerpo, headers=encabezados)

    def lista_spans(self) -> list[ReadableSpan]:
        return list(self.spans.get_finished_spans())


@pytest.fixture
def cotizacion(
    bases: Bases,
    wiremock: WireMock,
    catalogo_url: str,
    adaptador_url: str,
    registro: Registro,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., Cotizacion]]:
    """Fábrica: una app de cotizacion nueva por prueba; **cambios sobrescribe Settings."""
    from cotizacion.config import Settings
    from cotizacion.main import crear_app

    wiremock.limpiar_journal()
    wiremock.reiniciar_escenarios()
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level
    abiertos: list[TestClient] = []

    def crear(**cambios: Any) -> Cotizacion:
        valores: dict[str, Any] = {
            "SOLVENTA_DATABASE_URL": bases.cotizacion,
            "CATALOGO_URL": catalogo_url,
            "ADAPTADOR_DATOS_URL": adaptador_url,
            "SOLVENTA_CLIENTES_URL": f"{wiremock.url}/clientes-stub",
            "modelo_cache_ttl_s": 0,
            # Holgura para apps en hilos y contenedores en la CI; nunca sobre el corte de 700.
            "dependencia_timeout_ms": 1000,
            "fuente_timeout_ms": 700,
            **cambios,
        }
        spans = InMemorySpanExporter()
        app = crear_app(
            Settings(**valores), span_exporter=spans, metric_readers=[InMemoryMetricReader()]
        )
        # instrumentar reconfigura el logger raíz: la captura se instala después.
        salida = io.StringIO()
        handler = logging.StreamHandler(salida)
        handler.setFormatter(FormateadorJson("cotizacion", "pruebas"))
        raiz.handlers = [handler]
        raiz.setLevel(logging.INFO)
        cliente = TestClient(app)
        cliente.__enter__()
        abiertos.append(cliente)
        registro.logs.append(salida)
        registro.spans.append(spans)
        return Cotizacion(cliente, salida, spans, bases, wiremock)

    yield crear
    for cliente in abiertos:
        cliente.__exit__(None, None, None)
    raiz.handlers, raiz.level = anteriores, nivel
