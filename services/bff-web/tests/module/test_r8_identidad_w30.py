"""R8 (HU-W30), versión de módulo: del portal a la identidad del cliente, sin simular nada propio.

Corre en CI sin AWS. Cadena real de producción:
`bff-web` -> `clientes` (uvicorn + PostgreSQL de Testcontainers) -> `adaptador-identidad`
(uvicorn) -> proveedor KYC simulado (WireMock con los mapeos versionados de `simulador-aliados`).

El recorrido de perfilamiento exige la identidad VERIFICADA: la lee `cotizacion` con su propio
`ClientesHttp` contra `GET /v1/clientes/{id}/identidad`. Aquí se comprueba que ese consumidor
ve lo que deja el alta y la verificación: VERIFICADO habilita; RECHAZADO y PENDIENTE bloquean,
y ninguno de los reintentos duplica al cliente. Solo datos sintéticos.
"""

import asyncio
import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from bff_web.adapters.inbound import clientes as ruta
from bff_web.config import settings
from bff_web.main import app as portal_app
from cotizacion.adapters.outbound.clientes_http import ClientesHttp
from cotizacion.domain.perfilamiento.modelos import EstadoIdentidad
from fastapi.testclient import TestClient

docker = pytest.importorskip("docker")

SIMULADOR = Path(__file__).resolve().parents[3] / "simulador-aliados"
BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
ASESOR = {
    "X-Authenticated-Groups": "[asesores]",
    "X-Authenticated-Issuer": BACKOFFICE,
    "X-Authenticated-Sub": "sub-asesora",
}
# Cada número de documento activa un perfil del proveedor (mappings/kyc/documento-*.json).
NOMINAL, RECHAZADO, LENTO, MALFORMADO, CAIDO = (
    "1020456789",
    "1000000002",
    "1000000003",
    "1000000004",
    "1000000005",
)


class Servidor:
    """Una app ASGI servida por uvicorn en un hilo propio.

    psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: se usa un
    SelectorEventLoop propio (en Linux es el bucle por defecto).
    """

    def __init__(self, app: Any) -> None:
        self._zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._zocalo.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._zocalo.getsockname()[1]}"
        self._servidor = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        self._hilo = threading.Thread(target=self._correr, daemon=True)

    def _correr(self) -> None:
        asyncio.run(
            self._servidor.serve(sockets=[self._zocalo]), loop_factory=asyncio.SelectorEventLoop
        )

    def arrancar(self) -> "Servidor":
        self._hilo.start()
        limite = time.monotonic() + 30
        while not self._servidor.started:
            assert self._hilo.is_alive(), "el servidor no arrancó (ver su log)"
            assert time.monotonic() < limite, "el servidor no arrancó a tiempo"
            time.sleep(0.05)
        return self

    def apagar(self) -> None:
        self._servidor.should_exit = True
        self._hilo.join(timeout=15)


class WireMock:
    def __init__(self, url: str) -> None:
        self.url = url
        self._admin = httpx.Client(base_url=f"{url}/__admin", timeout=5)

    def solicitudes_a(self, sufijo: str) -> list[dict[str, Any]]:
        todas = self._admin.get("/requests").json()["requests"]
        return [r["request"] for r in todas if r["request"]["url"].endswith(sufijo)]

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


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from testcontainers.community.postgres import PostgresContainer

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    with PostgresContainer("postgres:16", driver=None) as contenedor:
        yield contenedor.get_connection_url()


@pytest.fixture(scope="module")
def wiremock(base: str) -> Iterator[WireMock]:
    from testcontainers.core.container import DockerContainer

    contenedor = (
        DockerContainer("wiremock/wiremock:3.9.2")
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


@pytest.fixture(scope="module")
def cadena(base: str, wiremock: WireMock) -> Iterator[Servidor]:
    """`clientes` real, cableado a un `adaptador-identidad` real que habla con WireMock."""
    from adaptador_identidad.config import Settings as AdaptadorSettings
    from adaptador_identidad.main import crear_app as crear_adaptador
    from clientes.config import Settings as ClientesSettings
    from clientes.main import crear_app as crear_clientes

    adaptador = Servidor(
        crear_adaptador(
            AdaptadorSettings(ALLY_KYC_URL=f"{wiremock.url}/kyc", ALLY_KYC_TIMEOUT_MS=500)
        )
    ).arrancar()
    clientes = Servidor(
        crear_clientes(
            ClientesSettings(
                SOLVENTA_DATABASE_URL=base,
                SOLVENTA_ADAPTADOR_IDENTIDAD_URL=adaptador.url,
                SOLVENTA_ADAPTADOR_IDENTIDAD_TIMEOUT_MS=2000,
            )
        )
    ).arrancar()
    yield clientes
    clientes.apagar()
    adaptador.apagar()


@pytest.fixture
def portal(
    cadena: Servidor, base: str, wiremock: WireMock, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")
    wiremock.limpiar_journal()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    monkeypatch.setattr(settings, "clientes_url", cadena.url)
    ruta.clientes.cache_clear()
    with TestClient(portal_app, headers=ASESOR) as cliente:
        yield cliente
    ruta.clientes.cache_clear()


def _alta(portal: TestClient, numero: str) -> httpx.Response:
    return portal.post(
        "/v1/clientes",
        json={
            "tipoDocumento": "CC",
            "numeroDocumento": numero,
            "nombreCompleto": "Natalia González Ruiz",
            "fechaNacimiento": "1990-04-18",
        },
    )


def _verificar(portal: TestClient, cliente_id: str) -> httpx.Response:
    return portal.post(f"/v1/clientes/{cliente_id}/identidad/verificaciones")


def _estado_para_perfilar(cadena: Servidor, cliente_id: str) -> EstadoIdentidad:
    """Lo que ve `cotizacion` al decidir si el perfilamiento puede iniciar."""

    async def leer() -> EstadoIdentidad:
        async with httpx.AsyncClient() as http:
            return await ClientesHttp(http, cadena.url, 2000).estado(uuid.UUID(cliente_id))

    return asyncio.run(leer())


def _clientes_en_base(base: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(base) as conexion:
        return conexion.execute(
            "SELECT estado, proveedor, referencia, verificado_en FROM cliente"
        ).fetchall()


def test_identidad_confirmada_habilita_el_recorrido(
    portal: TestClient, cadena: Servidor, base: str, wiremock: WireMock
) -> None:
    """AC1: PENDIENTE al crear; VERIFICADO con fecha y referencia tras la confirmación."""
    alta = _alta(portal, NOMINAL)
    assert alta.status_code == 201, alta.text
    cliente_id = alta.json()["clienteId"]
    assert _estado_para_perfilar(cadena, cliente_id) is EstadoIdentidad.PENDIENTE

    verificado = _verificar(portal, cliente_id)

    assert verificado.status_code == 200, verificado.text
    cuerpo = verificado.json()
    assert cuerpo["estado"] == "VERIFICADO"
    assert (cuerpo["proveedor"], cuerpo["referencia"]) == ("KYC", "kyc-doc-ref-0001")
    assert cuerpo["verificadoEn"] is not None
    assert _estado_para_perfilar(cadena, cliente_id) is EstadoIdentidad.VERIFICADO
    [(estado, proveedor, referencia, verificado_en)] = _clientes_en_base(base)
    assert (estado, proveedor, referencia) == ("VERIFICADO", "KYC", "kyc-doc-ref-0001")
    assert verificado_en is not None


def test_confirmacion_repetida_conserva_una_sola_identidad(
    portal: TestClient, base: str, wiremock: WireMock
) -> None:
    """AC4: repetir el alta y la verificación no duplica al cliente ni vuelve al proveedor."""
    cliente_id = _alta(portal, NOMINAL).json()["clienteId"]
    primera = _verificar(portal, cliente_id).json()
    segunda = _verificar(portal, cliente_id).json()
    repetida = _alta(portal, NOMINAL)

    assert primera == segunda
    assert (repetida.status_code, repetida.json()["clienteId"]) == (200, cliente_id)
    assert repetida.json()["estado"] == "VERIFICADO"
    assert len(_clientes_en_base(base)) == 1
    assert len(wiremock.solicitudes_a("/documento")) == 1


def test_identidad_rechazada_bloquea_sin_revelar_reglas(
    portal: TestClient, cadena: Servidor, base: str
) -> None:
    """AC2: RECHAZADO bloquea el perfilamiento; el portal no recibe el motivo del proveedor."""
    cliente_id = _alta(portal, RECHAZADO).json()["clienteId"]

    respuesta = _verificar(portal, cliente_id)

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "RECHAZADO"
    assert "DATOS_NO_COINCIDEN" not in respuesta.text
    assert _estado_para_perfilar(cadena, cliente_id) is EstadoIdentidad.RECHAZADO
    assert _alta(portal, RECHAZADO).json()["clienteId"] == cliente_id
    assert len(_clientes_en_base(base)) == 1


@pytest.mark.parametrize("numero", [LENTO, MALFORMADO, CAIDO], ids=["lento", "malformado", "caido"])
def test_falla_del_proveedor_deja_pendiente_y_bloquea_sin_rechazar(
    portal: TestClient, cadena: Servidor, base: str, numero: str
) -> None:
    """AC3: lento, malformado o caído es PENDIENTE y reintentable, nunca RECHAZADO."""
    cliente_id = _alta(portal, numero).json()["clienteId"]

    respuesta = _verificar(portal, cliente_id)

    assert respuesta.status_code == 503, respuesta.text
    assert respuesta.json()["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    assert respuesta.json()["detalles"]["estado"] == "PENDIENTE"
    assert _estado_para_perfilar(cadena, cliente_id) is EstadoIdentidad.PENDIENTE
    assert portal.get(f"/v1/clientes/{cliente_id}").json()["estado"] == "PENDIENTE"
    assert _verificar(portal, cliente_id).status_code == 503  # el reintento es posible
    assert [fila[0] for fila in _clientes_en_base(base)] == ["PENDIENTE"]


def test_un_cliente_inexistente_es_404(portal: TestClient) -> None:
    respuesta = _verificar(portal, str(uuid.uuid4()))
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "CLIENTE_NO_ENCONTRADO")


def test_el_portal_nunca_devuelve_pii(portal: TestClient) -> None:
    textos: list[str] = []
    for numero in (NOMINAL, RECHAZADO):
        alta = _alta(portal, numero)
        textos += [alta.text, _verificar(portal, alta.json()["clienteId"]).text]
    for texto in textos:
        for pii in ("Natalia", "1990-04-18", NOMINAL, RECHAZADO):
            assert pii not in texto
