"""Módulo de catalogo con PostgreSQL efímero: migraciones, semilla, inmutabilidad y vigencia."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from catalogo.adapters.outbound.modelos_pg import definicion_semilla, migrar
from catalogo.config import Settings
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.main import crear_app
from fastapi.testclient import TestClient

docker = pytest.importorskip("docker")
ADMIN = {"X-Actor-Id": "actuaria-sintetica", "X-Actor-Rol": "ADMIN_REGLAS"}
BASE = "/v1/modelos-riesgo"


@pytest.fixture(scope="module")
def contenedor() -> Iterator[Any]:
    # Ryuk no arranca en Docker Desktop.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from testcontainers.community.postgres import PostgresContainer

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    with PostgresContainer("postgres:16", driver=None) as postgres:
        yield postgres


@pytest.fixture(scope="module")
def base(contenedor: Any) -> str:
    url: str = contenedor.get_connection_url()
    migrar(url)
    return url


@pytest.fixture(scope="module")
def api(base: str) -> Iterator[TestClient]:
    # La app real: el lifespan vuelve a migrar y abre el pool.
    with TestClient(crear_app(Settings(SOLVENTA_DATABASE_URL=base))) as cliente:
        yield cliente


def _filas(url: str, sql: str, *parametros: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conexion:
        return conexion.execute(sql, parametros).fetchall()


def _publicar(api: TestClient, version: str, vigente_desde: datetime, **cambios: Any) -> Any:
    definicion = definicion_semilla()
    producto = cambios.pop("producto_id", "vida-hipotecario")
    definicion["productoId"] = producto
    for clave, valor in cambios.items():
        definicion[clave] = valor
    solicitud = {
        "version": version,
        "productoId": producto,
        "vigenteDesde": vigente_desde.isoformat(),
        "definicion": definicion,
    }
    return api.post(BASE, json=solicitud, headers=ADMIN)


def test_mt_w11_01_migrar_es_idempotente_y_concurrente(contenedor: Any, base: str) -> None:
    migrar(base)
    migrar(base)
    assert _filas(base, "SELECT count(*) FROM modelos_riesgo WHERE version = '2026.10.1'") == [(1,)]

    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("CREATE DATABASE catalogo_paralelo")
    paralela = base.rsplit("/", 1)[0] + "/catalogo_paralelo"
    with ThreadPoolExecutor(max_workers=2) as hilos:
        list(hilos.map(migrar, [paralela, paralela]))
    assert _filas(paralela, "SELECT version FROM modelos_riesgo") == [("2026.10.1",)]


def test_mt_w11_02_semilla_con_su_hash(api: TestClient, base: str) -> None:
    esperado = hash_canonico(definicion_semilla())
    assert _filas(
        base, "SELECT hash, publicado_por FROM modelos_riesgo WHERE version = %s", "2026.10.1"
    ) == [(esperado, "migracion")]
    cuerpo = api.get(f"{BASE}/2026.10.1").json()
    assert cuerpo["hash"] == esperado
    assert hash_canonico(cuerpo["definicion"]) == esperado
    assert cuerpo["vigenteDesde"] == "2026-10-01T00:00:00Z"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE modelos_riesgo SET hash = 'x' WHERE version = '2026.10.1'",
        "DELETE FROM modelos_riesgo WHERE version = '2026.10.1'",
    ],
    ids=["update", "delete"],
)
def test_mt_w11_03_modelos_inmutables(base: str, sql: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        _filas(base, sql)


def test_mt_w11_04_version_nueva_vigente_y_anterior_intacta(api: TestClient) -> None:
    anterior = api.get(f"{BASE}/2026.10.1").json()
    niveles = [
        {"desde": "80", "nivel": "BAJO"},
        {"desde": "50", "nivel": "MEDIO"},
        {"desde": "0", "nivel": "ALTO"},
    ]
    respuesta = _publicar(api, "2026.10.2", datetime.now(UTC), niveles=niveles)
    assert respuesta.status_code == 201, respuesta.text
    vigente = api.get(f"{BASE}/vigente", params={"productoId": "vida-hipotecario"}).json()
    assert vigente["version"] == "2026.10.2"
    assert vigente["definicion"]["niveles"][0]["desde"] == "80"
    assert api.get(f"{BASE}/2026.10.1").json() == anterior


def test_mt_w11_05_limite_de_vigencia(api: TestClient) -> None:
    desde = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=123456)
    assert _publicar(api, "2026.10.5", desde, producto_id="vida-limite").status_code == 201
    exacto = {"productoId": "vida-limite", "en": desde.isoformat()}
    antes = {"productoId": "vida-limite", "en": (desde - timedelta(microseconds=1)).isoformat()}
    assert api.get(f"{BASE}/vigente", params=exacto).json()["version"] == "2026.10.5"
    respuesta = api.get(f"{BASE}/vigente", params=antes)
    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "MODELO_NO_ENCONTRADO"


def test_mt_w11_06_version_repetida_responde_409(api: TestClient, base: str) -> None:
    desde = datetime.now(UTC) + timedelta(days=1)
    assert _publicar(api, "2026.10.6", desde, producto_id="vida-repetida").status_code == 201
    respuesta = _publicar(api, "2026.10.6", desde, producto_id="vida-repetida")
    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "VERSION_EXISTENTE"
    assert _filas(base, "SELECT count(*) FROM modelos_riesgo WHERE version = '2026.10.6'") == [(1,)]


def test_health_con_base(api: TestClient) -> None:
    assert api.get("/health").json() == {"status": "ok"}
