"""Arnés de módulo de cotizacion (W11-P14): lo usan W10-P12, W11-P14, W12-P08 y HU-W31.

- PostgreSQL 16 efímero con las bases `catalogo`, `cotizacion` y `clientes` (un solo contenedor):
  las dos primeras migradas con su `migrar`; la de `clientes` la migra su propio arranque.
- WireMock 3.9.2 con los mapeos versionados del simulador (aliados).
- Las apps reales de catalogo, adaptador-datos y `clientes` (HU-W31, PR 3.7: sustituye al doble
  de WireMock de las PR 3.6 y 3.4), servidas con uvicorn en hilos. Antes de cada prueba,
  `clientes` se siembra por SQL con los clientes sintéticos C01–C19
  (`contracts/datos-sinteticos/clientes-perfilamiento.json`): su identidad y un consentimiento
  que produce la decisión del caso. Solo `adaptador-identidad` es simulado (no se usa).
- cotizacion con TestClient, apuntada a todo lo anterior y sin caché del modelo.
- psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: los servidores en hilo y el
  TestClient usan un SelectorEventLoop (en Linux es el bucle por defecto), como `bff-movil`.
- Centinelas: los logs y spans de toda la suite se revisan al final (test_zz_centinelas.py).
"""

import asyncio
import io
import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
from starlette.types import ASGIApp, Receive, Scope, Send

docker = pytest.importorskip("docker")

RAIZ = Path(__file__).resolve().parents[4]
SIMULADOR = RAIZ / "services" / "simulador-aliados"
EVENTOS = RAIZ / "contracts" / "events"
CASOS: list[dict[str, Any]] = json.loads(
    (RAIZ / "contracts" / "datos-sinteticos" / "clientes-perfilamiento.json").read_text(
        encoding="utf-8"
    )
)
IMAGEN_WIREMOCK = "wiremock/wiremock:3.9.2"
PROPOSITO = "PERFILAMIENTO_PRECIO"
TODOS_LOS_ALCANCES = [
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
]
# Los instantes que respondía el doble de la PR 3.6: 12 meses de vigencia (ADR 0005).
VERIFICADO_EN = datetime(2026, 10, 1, tzinfo=UTC)
OTORGADO_EN = datetime(2026, 10, 1, tzinfo=UTC)
VIGENTE_HASTA = datetime(2027, 10, 1, tzinfo=UTC)
VERSION_TEXTO = "2026-10-17.1"

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


def sql(url: str, sentencia: str, *parametros: Any) -> list[tuple[Any, ...]]:
    """Una sentencia en su propia transacción; devuelve las filas si las hay."""
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.execute(sentencia, parametros)
        return cursor.fetchall() if cursor.description else []


@dataclass(frozen=True)
class Bases:
    catalogo: str
    cotizacion: str
    clientes: str

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
            conexion.execute("CREATE DATABASE clientes")
        base = url.rsplit("/", 1)[0]
        urls = Bases(
            catalogo=f"{base}/catalogo",
            cotizacion=f"{base}/cotizacion",
            clientes=f"{base}/clientes",
        )
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
        """Solicitudes a open-finance y datos-abiertos de un cliente."""
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
def servir(app: ASGIApp) -> Iterator[str]:
    """Sirve la app con uvicorn en un hilo (con su lifespan) y devuelve su URL."""
    puerto = puerto_libre()
    servidor = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=puerto, log_config=None, access_log=False)
    )
    hilo = threading.Thread(
        target=lambda: asyncio.run(servidor.serve(), loop_factory=asyncio.SelectorEventLoop),
        daemon=True,
    )
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
def servir_app() -> Callable[[FastAPI], Any]:
    """`servir` para las pruebas que necesitan una app propia que se detiene (MT-W12-12)."""
    return servir


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
        # Equivale a SOLVENTA_CB_FALLOS_CONSECUTIVOS=1000: el adaptador es de sesión y el
        # circuito no debe abrirse entre pruebas (W12-P08).
        cb_fallos_consecutivos=1000,
    )
    with servir(crear_adaptador(settings)) as url:
        yield url


class AntesDeConsultar:
    """ASGI delante de adaptador-datos: la primera consulta de una fuente (fase B) espera a que
    `accion` termine (en un hilo) antes de pasar; las demás esperan a esa primera.

    Es lo que ocurre en el mundo mientras las fuentes están en vuelo (por ejemplo, el titular
    revoca desde la app), sin ganchos en el código de producción.
    """

    def __init__(self, app: ASGIApp, accion: Callable[[], object]) -> None:
        self._app = app
        self._accion = accion
        self._candado = asyncio.Lock()
        self.ejecutada = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/v1/consultas":
            async with self._candado:
                if not self.ejecutada:
                    await asyncio.to_thread(self._accion)
                    self.ejecutada = True
        await self._app(scope, receive, send)


@pytest.fixture(scope="session")
def adaptador_con_accion(wiremock: WireMock) -> Callable[..., AbstractContextManager[str]]:
    """`adaptador_con_accion(accion)`: un adaptador-datos propio que ejecuta `accion` durante la
    fase B (ver `AntesDeConsultar`); devuelve su URL."""
    from adaptador_datos.config import Settings as SettingsAdaptador
    from adaptador_datos.main import crear_app as crear_adaptador

    @contextmanager
    def abrir(accion: Callable[[], object]) -> Iterator[str]:
        settings = SettingsAdaptador(
            ALLY_OPEN_FINANCE_URL=f"{wiremock.url}/open-finance",
            ALLY_DATOS_ABIERTOS_URL=f"{wiremock.url}/datos-abiertos",
            cb_fallos_consecutivos=1000,
        )
        interceptado = AntesDeConsultar(crear_adaptador(settings), accion)
        with servir(interceptado) as url:
            yield url
        assert interceptado.ejecutada, "ninguna fuente llegó al adaptador"

    return abrir


# --- clientes real (HU-W31, PR 3.7) -------------------------------------------------------


def nn_de(cliente: str) -> str:
    return cliente[-2:]


def sub_de(nn: str) -> str:
    """`sub` sintético del titular (lo que deja HU-M06); no es PII."""
    return f"sub-sintetico-{nn}"


def consentimiento_id(nn: str) -> str:
    """Identificador sintético del consentimiento de un caso (el de los datos sintéticos)."""
    return f"00000000-0000-4000-9000-0000000000{nn}"


class BloqueoOutbox:
    """Bloquea las escrituras en el outbox de `clientes` (las lecturas siguen): una verificación
    no puede registrar su decisión y no responde. Es una falla técnica real de `clientes`."""

    def __init__(self, base: str) -> None:
        self._base = base
        self._conexion: psycopg.Connection[Any] | None = None

    def tomar(self) -> None:
        self._conexion = psycopg.connect(self._base)
        self._conexion.execute("LOCK TABLE outbox IN EXCLUSIVE MODE")

    def soltar(self) -> None:
        if self._conexion is not None:
            self._conexion.rollback()
            self._conexion.close()
            self._conexion = None


@dataclass(frozen=True)
class Clientes:
    """`clientes` real sobre su base: siembra, lectura del outbox y su API de titular."""

    url: str
    base: str

    def sembrar(self, sin_consentimiento: Collection[str] = ()) -> None:
        """Deja la base con C01–C19 y nada más. Por caso: el cliente con su identidad y el
        consentimiento que produce su decisión (vigente con los alcances permitidos, revocado,
        `ACTIVO` ya vencido sin materializar, o ninguno). `sin_consentimiento`: casos (`nn`) que
        se siembran sin consentimiento, para otorgarlo por la API."""
        with psycopg.connect(self.base, autocommit=True) as conexion:
            conexion.execute(
                "TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente"
            )
            for caso in CASOS:
                nn = nn_de(caso["clienteId"])
                verificado = caso["identidad"] == "VERIFICADO"
                conexion.execute(
                    "INSERT INTO cliente (id, sub, estado, verificado_en) VALUES (%s, %s, %s, %s)",
                    (
                        caso["clienteId"],
                        sub_de(nn),
                        caso["identidad"],
                        VERIFICADO_EN if verificado else None,
                    ),
                )
                if nn not in sin_consentimiento:
                    self._sembrar_consentimiento(conexion, caso)

    @staticmethod
    def _sembrar_consentimiento(conexion: psycopg.Connection[Any], caso: dict[str, Any]) -> None:
        esperado = caso["consentimiento"]
        nn = nn_de(caso["clienteId"])
        alcances, estado = esperado["alcancesPermitidos"], "ACTIVO"
        otorgado, vigente, revocado = OTORGADO_EN, VIGENTE_HASTA, None
        if esperado["decision"] == "RECHAZADO":
            alcances = TODOS_LOS_ALCANCES
            if esperado["causa"] == "SIN_CONSENTIMIENTO":
                return
            if esperado["causa"] == "REVOCADO":
                estado, revocado = "REVOCADO", datetime(2026, 10, 5, tzinfo=UTC)
            elif esperado["causa"] == "VENCIDO":
                otorgado, vigente = (
                    datetime(2025, 9, 1, tzinfo=UTC),
                    datetime(2026, 9, 1, tzinfo=UTC),
                )
            else:
                raise AssertionError(f"causa sin siembra: {esperado['causa']}")
        conexion.execute(
            "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, "
            "idioma, estado, otorgado_en, vigente_hasta, revocado_en) "
            "VALUES (%s, %s, %s, %s, %s, 'es-CO', %s, %s, %s, %s)",
            (
                consentimiento_id(nn),
                caso["clienteId"],
                PROPOSITO,
                alcances,
                VERSION_TEXTO,
                estado,
                otorgado,
                vigente,
                revocado,
            ),
        )

    def filas(self, sentencia: str, *parametros: Any) -> list[tuple[Any, ...]]:
        return sql(self.base, sentencia, *parametros)

    def verificaciones(self, cliente: str) -> list[dict[str, Any]]:
        """Payload de cada `VerificacionConsentimientoRegistrada` del cliente, en orden: cada
        verificación que `clientes` recibió y decidió (ADR 0011)."""
        filas = self.filas(
            "SELECT payload FROM outbox WHERE aggregate_id = %s "
            "AND event_type = 'VerificacionConsentimientoRegistrada' ORDER BY occurred_at",
            cliente,
        )
        return [dict(payload) for (payload,) in filas]

    def esperar_verificaciones(self, cliente: str, cuantas: int) -> list[dict[str, Any]]:
        """Las verificaciones del cliente cuando ya son `cuantas` (las que quedaron esperando
        el outbox terminan al soltarlo)."""
        limite = time.monotonic() + 10
        while len(encontradas := self.verificaciones(cliente)) < cuantas:
            assert time.monotonic() < limite, encontradas
            time.sleep(0.05)
        return encontradas

    def bloqueo_outbox(self) -> BloqueoOutbox:
        return BloqueoOutbox(self.base)

    def _como_titular(self, nn: str, metodo: str, ruta: str, **kwargs: Any) -> httpx.Response:
        cabeceras = {
            "X-Actor-Id": sub_de(nn),
            "X-Actor-Rol": "CLIENTE",
            **kwargs.pop("headers", {}),
        }
        return httpx.request(metodo, f"{self.url}{ruta}", headers=cabeceras, timeout=10, **kwargs)

    def otorgar(self, nn: str, alcances: list[str]) -> dict[str, Any]:
        """Lo que hace la app (vía bff-movil) tras mostrar el texto vigente: 201 `ACTIVO`."""
        texto = httpx.get(
            f"{self.url}/v1/consentimientos/textos/vigente",
            params={"proposito": PROPOSITO, "idioma": "es-CO"},
            timeout=10,
        )
        assert texto.status_code == 200, texto.text
        respuesta = self._como_titular(
            nn,
            "POST",
            "/v1/clientes/actual/consentimientos",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "proposito": PROPOSITO,
                "alcances": alcances,
                "vigenciaMeses": 12,
                "versionTexto": texto.json()["versionTexto"],
                "idioma": "es-CO",
            },
        )
        assert respuesta.status_code == 201, respuesta.text
        return dict(respuesta.json())

    def revocar(self, nn: str, consentimiento: str) -> dict[str, Any]:
        """Lo que hace la app (vía bff-movil) al revocar: 200 `REVOCADO`."""
        respuesta = self._como_titular(
            nn, "POST", f"/v1/clientes/actual/consentimientos/{consentimiento}/revocacion"
        )
        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.json()["estado"] == "REVOCADO"
        return dict(respuesta.json())


@pytest.fixture(scope="session")
def clientes_real(bases: Bases) -> Iterator[Clientes]:
    """`clientes` tal como se despliega: migra su base y abre su `AsyncConnectionPool` al
    arrancar. Solo `adaptador-identidad` es simulado: la verificación no lo usa."""
    from clientes.config import Settings as SettingsClientes
    from clientes.main import crear_app as crear_clientes

    async def sin_llamadas(request: httpx.Request) -> httpx.Response:
        raise AssertionError("cotizacion no invoca a adaptador-identidad a través de clientes")

    app = crear_clientes(
        SettingsClientes(SOLVENTA_DATABASE_URL=bases.clientes),
        transport=httpx.MockTransport(sin_llamadas),
    )
    with servir(app) as url:
        yield Clientes(url=url, base=bases.clientes)


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
    lector: InMemoryMetricReader
    clientes: Clientes

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
    clientes_real: Clientes,
    registro: Registro,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., Cotizacion]]:
    """Fábrica: una app de cotizacion nueva por prueba; **cambios sobrescribe Settings.
    `clientes` se vuelve a sembrar con C01–C19 antes de cada prueba."""
    from cotizacion.config import Settings
    from cotizacion.main import crear_app

    wiremock.limpiar_journal()
    wiremock.reiniciar_escenarios()
    clientes_real.sembrar()
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
            "CLIENTES_URL": clientes_real.url,
            "modelo_cache_ttl_s": 0,
            # Holgura para apps en hilos y contenedores en la CI; nunca sobre el corte de 700.
            "dependencia_timeout_ms": 1000,
            "fuente_timeout_ms": 700,
            **cambios,
        }
        spans = InMemorySpanExporter()
        lector = InMemoryMetricReader()
        app = crear_app(Settings(**valores), span_exporter=spans, metric_readers=[lector])
        # instrumentar reconfigura el logger raíz: la captura se instala después.
        salida = io.StringIO()
        handler = logging.StreamHandler(salida)
        handler.setFormatter(FormateadorJson("cotizacion", "pruebas"))
        raiz.handlers = [handler]
        raiz.setLevel(logging.INFO)
        cliente = TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop})
        cliente.__enter__()
        abiertos.append(cliente)
        registro.logs.append(salida)
        registro.spans.append(spans)
        return Cotizacion(cliente, salida, spans, bases, wiremock, lector, clientes_real)

    yield crear
    for cliente in abiertos:
        cliente.__exit__(None, None, None)
    raiz.handlers, raiz.level = anteriores, nivel
