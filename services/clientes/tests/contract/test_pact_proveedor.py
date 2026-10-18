"""`clientes` verifica como proveedor el pacto de `cotizacion` (HU-W31, PR 3.2).

La app real, ensamblada como en producción (migra y abre su `AsyncConnectionPool`), contra un
Postgres de Testcontainers. Cada estado del proveedor se siembra con SQL desde el manejador de
estados de Pact, que corre dentro de la prueba: el código de producción no expone ninguna ruta de
prueba. El pacto no se edita aquí: lo genera `cotizacion` (`make test-contract SERVICE=cotizacion`).
"""

import asyncio
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from pact import Verifier

PACTO = Path(__file__).parents[4] / "contracts" / "pact" / "cotizacion-clientes.json"
CLIENTE = "00000000-0000-4000-8000-000000000001"
CONSENTIMIENTO = "00000000-0000-4000-9000-000000000001"
CUATRO = [
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
]
# Solo existe un propósito en el dominio: este valor sintético solo se siembra en la base (la
# columna no lleva CHECK) para el estado "ACTIVO para otro propósito".
OTRO_PROPOSITO = "PROPOSITO_SINTETICO_DE_PRUEBA"


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    docker = pytest.importorskip("docker")
    # Ryuk no arranca en Docker Desktop; en la CI ya viene deshabilitado.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from testcontainers.community.postgres import PostgresContainer

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    with PostgresContainer("postgres:16", driver=None) as contenedor:
        yield contenedor.get_connection_url()


@pytest.fixture(scope="module")
def proveedor(base: str) -> Iterator[str]:
    """`clientes` en un hilo propio con un SelectorEventLoop: psycopg asíncrono no corre sobre el
    ProactorEventLoop de Windows (en Linux es el bucle por defecto)."""
    from clientes.config import Settings
    from clientes.main import crear_app

    async def sin_llamadas(request: httpx.Request) -> httpx.Response:
        raise AssertionError("la verificación no invoca a adaptador-identidad")

    app = crear_app(
        Settings(SOLVENTA_DATABASE_URL=base), transport=httpx.MockTransport(sin_llamadas)
    )
    zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    zocalo.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{zocalo.getsockname()[1]}"
    servidor = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    def correr() -> None:
        asyncio.run(servidor.serve(sockets=[zocalo]), loop_factory=asyncio.SelectorEventLoop)

    hilo = threading.Thread(target=correr, daemon=True)
    hilo.start()
    limite = time.monotonic() + 30
    while not servidor.started:
        assert hilo.is_alive(), "clientes no arrancó (ver el log del servidor)"
        assert time.monotonic() < limite, "clientes no arrancó a tiempo"
        time.sleep(0.05)
    yield url
    servidor.should_exit = True
    hilo.join(timeout=15)


def _sql(base: str, sentencia: str, parametros: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(sentencia, parametros)


def _cliente(base: str) -> None:
    _sql(
        base,
        "INSERT INTO cliente (id, sub, estado, verificado_en) "
        "VALUES (%s, 'sub-pacto', 'VERIFICADO', clock_timestamp())",
        (CLIENTE,),
    )


def _consentimiento(
    base: str,
    *,
    estado: str = "ACTIVO",
    proposito: str = "PERFILAMIENTO_PRECIO",
    alcances: list[str] = CUATRO,
    vigente_por: str = "365 days",
) -> None:
    """Instantes relativos al reloj de la base, con una sola captura (ADR 0010)."""
    _sql(
        base,
        "WITH t AS MATERIALIZED (SELECT clock_timestamp() AS ahora) "
        "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, idioma, "
        "estado, otorgado_en, vigente_hasta, revocado_en) "
        "SELECT %s, %s, %s, %s, 'v-pacto', 'es-CO', %s, t.ahora - interval '400 days', "
        "t.ahora + %s::interval, CASE WHEN %s = 'REVOCADO' THEN t.ahora - interval '1 hour' END "
        "FROM t",
        (CONSENTIMIENTO, CLIENTE, proposito, alcances, estado, vigente_por, estado),
    )


def _sembrar(base: str, estado: str) -> None:
    """Un estado por interacción del pacto; uno desconocido falla la verificación."""
    _sql(base, "TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")
    sin_ingresos = [a for a in CUATRO if a != "FA_INGRESOS_AGREGADOS"]
    sembrados = {
        f"el cliente {CLIENTE} tiene un consentimiento ACTIVO y vigente para PERFILAMIENTO_PRECIO"
        " con los cuatro alcances": {},
        f"el cliente {CLIENTE} tiene un consentimiento ACTIVO y vigente para PERFILAMIENTO_PRECIO"
        " sin el alcance FA_INGRESOS_AGREGADOS": {"alcances": sin_ingresos},
        f"el cliente {CLIENTE} existe y no tiene consentimientos": None,
        f"el cliente {CLIENTE} revocó su consentimiento para PERFILAMIENTO_PRECIO y no tiene otro"
        " ACTIVO": {"estado": "REVOCADO"},
        # ACTIVO almacenado y ya vencido sin materializar: el caso que deriva la lectura.
        f"el consentimiento del cliente {CLIENTE} para PERFILAMIENTO_PRECIO llegó a su"
        " vigenteHasta y no tiene otro ACTIVO": {"vigente_por": "-1 second"},
        f"el cliente {CLIENTE} solo tiene un consentimiento ACTIVO para otro propósito": {
            "proposito": OTRO_PROPOSITO
        },
    }
    if estado == "el cliente 00000000-0000-4000-8000-0000000000ff no existe":
        return
    if estado not in sembrados:
        raise AssertionError(f"estado del proveedor sin sembrar: {estado!r}")
    _cliente(base)
    datos = sembrados[estado]
    if datos is not None:
        _consentimiento(base, **datos)


def test_clientes_cumple_el_pacto_de_cotizacion(base: str, proveedor: str) -> None:
    assert PACTO.exists(), "Ejecutar primero services/cotizacion/tests/contract (genera el pacto)"
    vistos: list[str] = []

    def estado(state: str, action: str) -> None:
        if action == "setup":
            vistos.append(state)
            _sembrar(base, state)

    Verifier("clientes", host="127.0.0.1").add_source(PACTO).add_transport(
        url=proveedor
    ).state_handler(estado, teardown=True).verify()

    assert len(vistos) == 7  # una por interacción del pacto


def test_cada_verificacion_del_pacto_quedo_registrada(base: str, proveedor: str) -> None:
    """Con la app real, una verificación deja su evento en el outbox; un 404 no deja ninguno."""
    _sembrar(
        base,
        f"el cliente {CLIENTE} tiene un consentimiento ACTIVO y vigente para PERFILAMIENTO_PRECIO"
        " con los cuatro alcances",
    )
    cuerpo = {"proposito": "PERFILAMIENTO_PRECIO", "alcances": CUATRO}
    ruta = "/v1/clientes/{}/consentimientos/verificaciones"

    assert httpx.post(proveedor + ruta.format(CLIENTE), json=cuerpo).status_code == 200
    ausente = "00000000-0000-4000-8000-0000000000ff"
    assert httpx.post(proveedor + ruta.format(ausente), json=cuerpo).status_code == 404

    with psycopg.connect(base) as conexion:
        filas = conexion.execute(
            "SELECT aggregate_id::text, event_type FROM outbox WHERE published_at IS NULL"
        ).fetchall()
    assert filas == [(CLIENTE, "VerificacionConsentimientoRegistrada")]
