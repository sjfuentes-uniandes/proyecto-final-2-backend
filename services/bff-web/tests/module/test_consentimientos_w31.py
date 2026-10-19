"""HU-W31 (PR 3.5), versión de módulo: el asesor lee los consentimientos del cliente.

Cadena real: `bff-web` -> `clientes` (uvicorn + PostgreSQL de Testcontainers). Cubre la frontera
nueva `bff-web` -> `clientes` contra el proveedor real (no hay Pact `bff-web` -> `clientes`;
como en HU-W30, los DTO se cotejan con `clientes.yaml` en tests/contract/test_openapi.py).
Los consentimientos se siembran por SQL: el asesor no otorga ni revoca (ADR 0002). Solo datos
sintéticos.
"""

import asyncio
import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from bff_web.adapters.inbound import consentimientos as ruta
from bff_web.config import settings
from bff_web.main import app as portal_app
from fastapi.testclient import TestClient

docker = pytest.importorskip("docker")

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
ASESOR = {
    "X-Authenticated-Groups": "[asesores]",
    "X-Authenticated-Issuer": BACKOFFICE,
    "X-Authenticated-Sub": "sub-asesora",
}
# Lo que el BFF envía a clientes por el asesor.
ACTOR = {"X-Actor-Id": "sub-asesora", "X-Actor-Rol": "ASESOR"}
PROPOSITO = "PERFILAMIENTO_PRECIO"
CON_HISTORIA = uuid.UUID("00000000-0000-4000-8000-000000003101")
VENCIDO = uuid.UUID("00000000-0000-4000-8000-000000003102")
SIN_CONSENTIMIENTOS = uuid.UUID("00000000-0000-4000-8000-000000003103")
REVOCADO_ID = uuid.UUID("00000000-0000-4000-9000-000000003101")
ACTIVO_ID = uuid.UUID("00000000-0000-4000-9000-000000003102")
VENCIDO_ID = uuid.UUID("00000000-0000-4000-9000-000000003103")


class Servidor:
    """Una app ASGI servida por uvicorn en un hilo propio (igual que en test_r8_identidad_w30).

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
def clientes(base: str) -> Iterator[Servidor]:
    """`clientes` tal como se despliega: migra su base al arrancar."""
    from clientes.config import Settings as ClientesSettings
    from clientes.main import crear_app as crear_clientes

    servidor = Servidor(crear_clientes(ClientesSettings(SOLVENTA_DATABASE_URL=base))).arrancar()
    yield servidor
    servidor.apagar()


def _sembrar(base: str) -> None:
    """CON_HISTORIA: un REVOCADO y, después, un ACTIVO vigente. VENCIDO: un ACTIVO cuya vigencia
    ya pasó, sin materializar. SIN_CONSENTIMIENTOS: solo el cliente."""
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")
        for cliente_id in (CON_HISTORIA, VENCIDO, SIN_CONSENTIMIENTOS):
            conexion.execute(
                "INSERT INTO cliente (id, sub, estado, verificado_en) "
                "VALUES (%s, %s, 'VERIFICADO', clock_timestamp())",
                (cliente_id, f"sub-{cliente_id}"),
            )
        for consentimiento_id, cliente_id, estado, otorgado, vigente, revocado in (
            (REVOCADO_ID, CON_HISTORIA, "REVOCADO", "-60 days", "+305 days", "-30 days"),
            (ACTIVO_ID, CON_HISTORIA, "ACTIVO", "-1 day", "+364 days", None),
            (VENCIDO_ID, VENCIDO, "ACTIVO", "-400 days", "-35 days", None),
        ):
            conexion.execute(
                "WITH t AS MATERIALIZED (SELECT clock_timestamp() AS ahora) "
                "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, "
                "idioma, estado, otorgado_en, vigente_hasta, revocado_en) "
                "SELECT %s, %s, %s, %s, 'v-modulo', 'es-CO', %s, t.ahora + %s::interval, "
                "t.ahora + %s::interval, t.ahora + %s::interval FROM t",
                (
                    consentimiento_id,
                    cliente_id,
                    PROPOSITO,
                    ["FA_PRODUCTOS_VIGENTES", "DA_REGISTROS_PUBLICOS"],
                    estado,
                    otorgado,
                    vigente,
                    revocado,
                ),
            )


@pytest.fixture
def portal(clientes: Servidor, base: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _sembrar(base)
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    monkeypatch.setattr(settings, "clientes_url", clientes.url)
    ruta.consentimientos.cache_clear()
    with TestClient(portal_app, headers=ASESOR) as cliente:
        yield cliente
    ruta.consentimientos.cache_clear()


def _directo(clientes: Servidor, ruta_clientes: str) -> httpx.Response:
    return httpx.get(f"{clientes.url}{ruta_clientes}", headers=ACTOR, timeout=5)


def _filas(base: str, sql: str, *parametros: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(base) as conexion:
        return conexion.execute(sql, parametros).fetchall()


def test_la_lista_del_asesor_es_la_de_clientes(portal: TestClient, clientes: Servidor) -> None:
    """Del más reciente al más antiguo; las mismas claves y valores que responde clientes."""
    ruta_lista = f"/v1/clientes/{CON_HISTORIA}/consentimientos"

    respuesta = portal.get(ruta_lista)

    assert respuesta.status_code == 200, respuesta.text
    assert [(c["consentimientoId"], c["estado"]) for c in respuesta.json()] == [
        (str(ACTIVO_ID), "ACTIVO"),
        (str(REVOCADO_ID), "REVOCADO"),
    ]
    assert respuesta.json()[0]["revocadoEn"] is None
    assert respuesta.json() == _directo(clientes, ruta_lista).json()


def test_el_vencido_lo_deriva_clientes_sin_escribir(
    portal: TestClient, clientes: Servidor, base: str
) -> None:
    """VENCIDO derivado al leer (ADR 0009); la lectura no materializa ni escribe en el outbox."""
    respuesta = portal.get(f"/v1/clientes/{VENCIDO}/consentimientos")
    historial = portal.get(f"/v1/clientes/{VENCIDO}/consentimientos/{VENCIDO_ID}/historial")

    assert [c["estado"] for c in respuesta.json()] == ["VENCIDO"]
    assert historial.status_code == 200, historial.text
    assert historial.json()["estado"] == "VENCIDO"
    assert [h["tipo"] for h in historial.json()["hitos"]] == ["OTORGADO", "VENCIDO"]
    assert _filas(base, "SELECT estado FROM consentimiento WHERE cliente_id = %s", VENCIDO) == [
        ("ACTIVO",)
    ]
    assert _filas(base, "SELECT count(*) FROM outbox") == [(0,)]


def test_el_historial_es_el_de_clientes(portal: TestClient, clientes: Servidor) -> None:
    ruta_historial = f"/v1/clientes/{CON_HISTORIA}/consentimientos/{REVOCADO_ID}/historial"

    respuesta = portal.get(ruta_historial)

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "REVOCADO"
    assert [h["tipo"] for h in respuesta.json()["hitos"]] == ["OTORGADO", "REVOCADO"]
    assert respuesta.json() == _directo(clientes, ruta_historial).json()


def test_una_revocacion_se_ve_en_la_siguiente_lectura(portal: TestClient, base: str) -> None:
    """Sin caché (ADR 0011): la revocación del titular se ve en la siguiente consulta."""
    ruta_lista = f"/v1/clientes/{CON_HISTORIA}/consentimientos"
    assert portal.get(ruta_lista).json()[0]["estado"] == "ACTIVO"
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(
            "UPDATE consentimiento SET estado = 'REVOCADO', revocado_en = clock_timestamp() "
            "WHERE id = %s",
            (ACTIVO_ID,),
        )

    assert portal.get(ruta_lista).json()[0]["estado"] == "REVOCADO"


def test_cliente_sin_consentimientos_es_lista_vacia(portal: TestClient) -> None:
    respuesta = portal.get(f"/v1/clientes/{SIN_CONSENTIMIENTOS}/consentimientos")
    assert (respuesta.status_code, respuesta.json()) == (200, [])


def test_cliente_inexistente_es_404(portal: TestClient) -> None:
    respuesta = portal.get(f"/v1/clientes/{uuid.uuid4()}/consentimientos")
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "CLIENTE_NO_ENCONTRADO")


@pytest.mark.parametrize(
    ("cliente_id", "consentimiento_id"),
    [
        (CON_HISTORIA, uuid.uuid4()),
        (SIN_CONSENTIMIENTOS, ACTIVO_ID),
        (uuid.uuid4(), ACTIVO_ID),
    ],
    ids=["inexistente", "de-otro-cliente", "cliente-inexistente"],
)
def test_historial_ajeno_o_inexistente_es_404_sin_distinguir(
    portal: TestClient, cliente_id: uuid.UUID, consentimiento_id: uuid.UUID
) -> None:
    respuesta = portal.get(
        f"/v1/clientes/{cliente_id}/consentimientos/{consentimiento_id}/historial"
    )
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (
        404,
        "CONSENTIMIENTO_NO_ENCONTRADO",
    )
    assert str(ACTIVO_ID) not in respuesta.text
