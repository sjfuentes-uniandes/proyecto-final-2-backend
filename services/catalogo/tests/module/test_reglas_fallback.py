"""N2 HU-W06: reglas de fallback con PostgreSQL efímero.

Migración y semilla idempotentes (validada contra el modelo sembrado), versión única por producto,
inmutabilidad, vigencia con vencimiento, migración adelante y atrás, y la API sobre la base real.
"""

import os
from collections.abc import Iterator
from importlib import resources
from typing import Any

import psycopg
import pytest
from catalogo.adapters.outbound.modelos_pg import migrar
from catalogo.config import Settings
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.main import crear_app
from fastapi.testclient import TestClient

docker = pytest.importorskip("docker")
MIGRACIONES = resources.files("catalogo.adapters.outbound").joinpath("migraciones")
_INSERTAR_COPIA = (
    "INSERT INTO reglas_fallback (producto_id, version, vigente_desde, vigente_hasta, definicion, "
    "hash, publicado_por) SELECT producto_id, %s, %s, %s, definicion, hash, 'prueba' "
    "FROM reglas_fallback WHERE version = '2026.10.1'"
)


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from testcontainers.community.postgres import PostgresContainer

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    with PostgresContainer("postgres:16", driver=None) as postgres:
        url: str = postgres.get_connection_url()
        migrar(url)
        yield url


def _filas(url: str, sql: str, *parametros: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.execute(sql, parametros)
        return cursor.fetchall() if cursor.description else []


def test_semilla_idempotente_con_su_hash(base: str) -> None:
    migrar(base)  # segunda vez: no duplica ni falla
    filas = _filas(
        base,
        "SELECT producto_id, version, vigente_hasta, definicion, hash, publicado_por "
        "FROM reglas_fallback",
    )
    assert len(filas) == 1
    producto, version, hasta, definicion, hash_, publicado_por = filas[0]
    assert (producto, version, hasta, publicado_por) == (
        "vida-hipotecario",
        "2026.10.1",
        None,
        "migracion",
    )
    assert hash_ == hash_canonico(definicion)


def test_una_version_repetida_no_se_publica_dos_veces(base: str) -> None:
    with pytest.raises(psycopg.errors.UniqueViolation):
        _filas(base, _INSERTAR_COPIA, "2026.10.1", "2026-10-01T00:00:00Z", None)


def test_reglas_de_fallback_inmutables(base: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        _filas(base, "UPDATE reglas_fallback SET publicado_por = 'otro'")
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        _filas(base, "DELETE FROM reglas_fallback")


def test_restricciones(base: str) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _filas(
            base,
            "INSERT INTO reglas_fallback (producto_id, version, vigente_desde, definicion, "
            "hash, publicado_por) VALUES ('no-existe', '2026.10.1', now(), '{}', 'h', 'p')",
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        _filas(base, _INSERTAR_COPIA, "2026.10.9", "2026-10-05T00:00:00Z", "2026-10-05T00:00:00Z")


def test_api_sobre_la_base_real_con_vencimiento(base: str) -> None:
    """Una versión temporal (10 al 12 de octubre) reemplaza a la semilla mientras rige; al vencer
    no hay reglas vigentes: no se vuelve a la anterior."""
    _filas(base, _INSERTAR_COPIA, "2026.10.2", "2026-10-10T00:00:00Z", "2026-10-12T00:00:00Z")
    ruta = "/v1/productos/vida-hipotecario/reglas-fallback"
    with TestClient(crear_app(Settings(SOLVENTA_DATABASE_URL=base))) as api:
        antes = api.get(f"{ruta}/vigente", params={"fecha": "2026-10-09T00:00:00Z"})
        durante = api.get(f"{ruta}/vigente", params={"fecha": "2026-10-11T00:00:00Z"})
        vencida = api.get(f"{ruta}/vigente", params={"fecha": "2026-10-12T00:00:00Z"})
        version = api.get(f"{ruta}/2026.10.1")
        sin_reglas = api.get("/v1/productos/vida-hipotecario-2025/reglas-fallback/vigente")
        inexistente = api.get("/v1/productos/no-existe/reglas-fallback/vigente")
    assert antes.json()["version"] == "2026.10.1"
    assert antes.json()["hash"] == hash_canonico(antes.json()["definicion"])
    assert durante.json()["version"] == "2026.10.2"
    assert (vencida.status_code, vencida.json()["codigo"]) == (404, "REGLAS_NO_VIGENTES")
    assert version.json() == antes.json()
    assert sin_reglas.json()["codigo"] == "REGLAS_NO_VIGENTES"
    assert inexistente.json()["codigo"] == "PRODUCTO_NO_ENCONTRADO"


def test_migracion_adelante_y_atras(base: str) -> None:
    """Revertir 004 borra la tabla; volver a migrar la recrea con la semilla."""
    reversa = MIGRACIONES.joinpath("down", "004_reglas_fallback.sql").read_text(encoding="utf-8")
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(reversa)
        conexion.execute(reversa)  # idempotente
    existe = "SELECT to_regclass('reglas_fallback') IS NOT NULL"
    assert _filas(base, existe) == [(False,)]
    migrar(base)
    assert _filas(base, existe) == [(True,)]
    assert _filas(base, "SELECT count(*) FROM reglas_fallback") == [(1,)]
