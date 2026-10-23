"""N2 HU-W13 (MT-W13-02, MT-W13-03): reglas de suscripción con PostgreSQL efímero.

Migración y semilla idempotentes, versión única por producto (una versión repetida no se publica
dos veces), inmutabilidad, migración adelante y atrás, y la API sobre la base real.
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
        return conexion.execute(sql, parametros).fetchall()


def test_semilla_idempotente_con_su_hash(base: str) -> None:
    migrar(base)  # segunda vez: no duplica ni falla
    filas = _filas(
        base, "SELECT producto_id, version, definicion, hash, publicado_por FROM reglas_suscripcion"
    )
    assert len(filas) == 1
    producto, version, definicion, hash_, publicado_por = filas[0]
    assert (producto, version, publicado_por) == ("vida-hipotecario", "2026.10.1", "migracion")
    assert hash_ == hash_canonico(definicion)


def test_una_version_repetida_no_se_publica_dos_veces(base: str) -> None:
    with pytest.raises(psycopg.errors.UniqueViolation):
        _filas(
            base,
            "INSERT INTO reglas_suscripcion (producto_id, version, vigente_desde, definicion, "
            "hash, publicado_por) SELECT producto_id, version, vigente_desde, definicion, hash, "
            "'otro' FROM reglas_suscripcion",
        )


def test_reglas_de_suscripcion_inmutables(base: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        _filas(base, "UPDATE reglas_suscripcion SET publicado_por = 'otro'")
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        _filas(base, "DELETE FROM reglas_suscripcion")


def test_restricciones(base: str) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _filas(
            base,
            "INSERT INTO reglas_suscripcion (producto_id, version, vigente_desde, definicion, "
            "hash, publicado_por) VALUES ('no-existe', '2026.10.1', now(), '{}', 'h', 'p')",
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        _filas(
            base,
            "INSERT INTO reglas_suscripcion (producto_id, version, vigente_desde, definicion, "
            "hash, publicado_por) VALUES ('vida-hipotecario', 'v2', now(), '{}', 'h', 'p')",
        )


def test_api_sobre_la_base_real(base: str) -> None:
    with TestClient(crear_app(Settings(SOLVENTA_DATABASE_URL=base))) as api:
        vigente = api.get(
            "/v1/productos/vida-hipotecario/reglas-suscripcion/vigente",
            params={"fecha": "2026-10-15T15:00:00Z"},
        )
        antes = api.get(
            "/v1/productos/vida-hipotecario/reglas-suscripcion/vigente",
            params={"fecha": "2026-09-30T23:59:59Z"},
        )
        version = api.get("/v1/productos/vida-hipotecario/reglas-suscripcion/2026.10.1")
        sin_reglas = api.get("/v1/productos/vida-hipotecario-2025/reglas-suscripcion/vigente")
        inexistente = api.get("/v1/productos/no-existe/reglas-suscripcion/vigente")
    assert vigente.status_code == 200
    assert vigente.json()["hash"] == hash_canonico(vigente.json()["definicion"])
    assert version.json() == vigente.json()
    assert (antes.status_code, antes.json()["codigo"]) == (404, "REGLAS_NO_VIGENTES")
    assert sin_reglas.json()["codigo"] == "REGLAS_NO_VIGENTES"
    assert inexistente.json()["codigo"] == "PRODUCTO_NO_ENCONTRADO"


def test_migracion_adelante_y_atras(base: str) -> None:
    """MT-W13-02: revertir 003 borra la tabla; volver a migrar la recrea con la semilla."""
    reversa = MIGRACIONES.joinpath("down", "003_reglas_suscripcion.sql").read_text(encoding="utf-8")
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(reversa)
        conexion.execute(reversa)  # idempotente
    existe = "SELECT to_regclass('reglas_suscripcion') IS NOT NULL"
    assert _filas(base, existe) == [(False,)]
    migrar(base)
    assert _filas(base, existe) == [(True,)]
    assert _filas(base, "SELECT count(*) FROM reglas_suscripcion") == [(1,)]
