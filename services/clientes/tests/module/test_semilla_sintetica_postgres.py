"""Pruebas de módulo de la semilla de clientes sintéticos (DEF-01) contra un Postgres real.

La app se ensambla como en `int` (sin repositorios inyectados): migra, siembra si
`CLIENTES_SEMILLA_SINTETICA` es true y abre el pool. Las decisiones se piden por la misma API que
usa `cotizacion` (`POST /v1/clientes/{id}/consentimientos/verificaciones`), con el reloj real de
la base: la vigencia sembrada llega hasta 2027-10-01.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest
from clientes.adapters.outbound.postgres import migrar
from clientes.adapters.outbound.semilla_sintetica import CLIENTES_SINTETICOS, sembrar
from clientes.config import Settings
from clientes.main import crear_app
from fastapi import FastAPI

docker = pytest.importorskip("docker")

pytestmark = pytest.mark.anyio

ID = {c.codigo: str(c.id) for c in CLIENTES_SINTETICOS}
ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
CUATRO = [
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
]
SOLICITUD = {"proposito": "PERFILAMIENTO_PRECIO", "alcances": CUATRO}


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # psycopg async no corre sobre el ProactorEventLoop de Windows.
    return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    # Ryuk no arranca en Docker Desktop.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from testcontainers.community.postgres import PostgresContainer

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    with PostgresContainer("postgres:16", driver=None) as contenedor:
        yield contenedor.get_connection_url()


@pytest.fixture(autouse=True)
def limpia(base: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_DATABASE_URL", base)
    migrar(base)
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")


def _sql(base: str, sentencia: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(base, autocommit=True) as conexion:
        return conexion.execute(sentencia).fetchall()


def _conteos(base: str) -> tuple[int, int]:
    ((clientes, consentimientos),) = _sql(
        base, "SELECT (SELECT count(*) FROM cliente), (SELECT count(*) FROM consentimiento)"
    )
    return clientes, consentimientos


def _ensamblar(semilla: bool) -> FastAPI:
    async def sin_llamadas(request: httpx.Request) -> httpx.Response:
        raise AssertionError("la semilla no invoca a adaptador-identidad")

    return crear_app(
        Settings(CLIENTES_SEMILLA_SINTETICA=semilla), transport=httpx.MockTransport(sin_llamadas)
    )


async def _arrancar_y_detener(semilla: bool) -> None:
    app = _ensamblar(semilla)
    async with app.router.lifespan_context(app):
        pass


@pytest.fixture
async def api() -> AsyncIterator[httpx.AsyncClient]:
    app = _ensamblar(semilla=True)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://clientes",
            headers={"X-Correlation-Id": "corr-modulo-def01"},
        ) as cliente,
    ):
        yield cliente


async def test_con_la_bandera_en_false_no_siembra_nada(base: str) -> None:
    await _arrancar_y_detener(semilla=False)

    assert _conteos(base) == (0, 0)


async def test_el_arranque_siembra_y_es_idempotente(base: str) -> None:
    await _arrancar_y_detener(semilla=True)
    assert _conteos(base) == (27, 26)  # C10 no tiene consentimiento

    await _arrancar_y_detener(semilla=True)

    assert _conteos(base) == (27, 26)
    assert sembrar(base) == 0
    assert _sql(base, "SELECT count(*) FROM outbox") == [(0,)]


def test_dos_replicas_a_la_vez_dejan_una_sola_semilla(base: str) -> None:
    with ThreadPoolExecutor(max_workers=2) as hilos:
        creados = list(hilos.map(sembrar, [base, base]))

    assert sorted(creados) == [0, 27]
    assert _conteos(base) == (27, 26)


async def test_no_pisa_lo_que_cambio_en_el_ambiente(base: str) -> None:
    sembrar(base)
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(
            "UPDATE consentimiento SET estado = 'REVOCADO', revocado_en = clock_timestamp() "
            "WHERE cliente_id = %s",
            (ID["C01"],),
        )

    sembrar(base)

    assert _sql(base, f"SELECT estado FROM consentimiento WHERE cliente_id = '{ID['C01']}'") == [
        ("REVOCADO",)
    ]


@pytest.mark.parametrize(
    ("codigo", "decision", "causa", "permitidos"),
    [
        ("C01", "PERMITIDO", None, CUATRO),
        ("C02", "PERMITIDO_PARCIAL", None, [a for a in CUATRO if a != "FA_INGRESOS_AGREGADOS"]),
        ("C08", "RECHAZADO", "REVOCADO", []),
        ("C09", "RECHAZADO", "VENCIDO", []),
        ("C10", "RECHAZADO", "SIN_CONSENTIMIENTO", []),
        ("C16", "PERMITIDO_PARCIAL", None, [a for a in CUATRO if a != "FA_HISTORIAL_PAGOS_12M"]),
        ("C17", "PERMITIDO_PARCIAL", None, ["DA_REGISTROS_PUBLICOS"]),
        # HU-W06: los casos de fallback tienen todos los alcances; falla la fuente, no el permiso.
        ("C20", "PERMITIDO", None, CUATRO),
        ("C27", "PERMITIDO", None, CUATRO),
    ],
)
async def test_la_verificacion_devuelve_la_decision_del_caso(
    api: httpx.AsyncClient,
    codigo: str,
    decision: str,
    causa: str | None,
    permitidos: list[str],
) -> None:
    """01 §3.5: la decisión y la causa que `cotizacion` espera de cada caso."""
    respuesta = await api.post(
        f"/v1/clientes/{ID[codigo]}/consentimientos/verificaciones", json=SOLICITUD
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert (cuerpo["decision"], cuerpo["causa"]) == (decision, causa)
    assert cuerpo["alcancesPermitidos"] == permitidos
    assert [r["alcance"] for r in cuerpo["alcancesRechazados"]] == [
        a for a in CUATRO if a not in permitidos
    ]
    if decision == "RECHAZADO":
        assert (cuerpo["consentimientoId"], cuerpo["vigenteHasta"]) == (None, None)
    else:
        assert cuerpo["consentimientoId"] == f"00000000-0000-4000-9000-0000000000{ID[codigo][-2:]}"
        vigente_hasta = datetime.fromisoformat(cuerpo["vigenteHasta"])
        assert vigente_hasta >= datetime(2027, 10, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("codigo", "estado", "con_fecha"),
    [("C01", "VERIFICADO", True), ("C11", "RECHAZADO", False), ("C12", "PENDIENTE", False)],
)
async def test_la_identidad_que_consulta_cotizacion(
    api: httpx.AsyncClient, codigo: str, estado: str, con_fecha: bool
) -> None:
    cuerpo = (await api.get(f"/v1/clientes/{ID[codigo]}/identidad")).json()

    assert (cuerpo["clienteId"], cuerpo["estado"]) == (ID[codigo], estado)
    assert (cuerpo["verificadoEn"] is not None) is con_fecha


async def test_el_alta_por_documento_reutiliza_el_uuid_de_c01(api: httpx.AsyncClient) -> None:
    """Paso 1 del portal: el documento de C01 devuelve C01, ya verificado, sin crear otro."""
    alta = {
        "tipoDocumento": "CC",
        "numeroDocumento": "9000000001",
        "nombreCompleto": "Otro nombre escrito por el asesor",
        "fechaNacimiento": "1990-04-18",
    }

    respuesta = await api.post("/v1/clientes", json=alta, headers=ASESOR)

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["clienteId"] == ID["C01"]
    assert respuesta.json()["estado"] == "VERIFICADO"
    verificacion = await api.post(
        f"/v1/clientes/{ID['C01']}/identidad/verificacion-documental", headers=ASESOR
    )
    assert verificacion.json()["estado"] == "VERIFICADO"  # no vuelve a consultar al proveedor
