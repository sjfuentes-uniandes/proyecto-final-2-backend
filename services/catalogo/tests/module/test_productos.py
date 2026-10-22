"""N2 HU-W03: catalogo con PostgreSQL y Valkey efímeros.

Migraciones y semilla de productos (idempotentes), inmutabilidad de las reglas de prima, la API
/v1/productos sobre la base real y el Cache-Aside: catalogo llena la caché al leer y la invalida al
arrancar (diagramas: catálogo actualiza / invalida).
"""

import json
import os
import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from catalogo.adapters.outbound.modelos_pg import migrar
from catalogo.adapters.outbound.productos_cache import clave_producto, clave_reglas_vigentes
from catalogo.config import Settings
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.main import crear_app
from fastapi.testclient import TestClient

docker = pytest.importorskip("docker")
IMAGEN_VALKEY = "valkey/valkey:8.0.2-alpine"


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


@pytest.fixture(scope="module")
def valkey() -> Iterator[str]:
    import redis
    from testcontainers.core.container import DockerContainer

    with DockerContainer(IMAGEN_VALKEY).with_exposed_ports(6379) as contenedor:
        url = f"redis://{contenedor.get_container_host_ip()}:{contenedor.get_exposed_port(6379)}"
        limite = time.monotonic() + 30
        while True:
            try:
                if redis.Redis.from_url(url).ping():
                    break
            except redis.RedisError:
                if time.monotonic() > limite:
                    raise
                time.sleep(0.2)
        yield url


def _filas(url: str, sql: str, *parametros: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.execute(sql, parametros).fetchall()


def test_migracion_y_semilla_idempotentes(base: str) -> None:
    migrar(base)  # segunda vez: no duplica ni falla
    productos = _filas(base, "SELECT id, moneda, estado FROM productos ORDER BY id")
    assert productos == [
        ("vida-hipotecario", "COP", "ACTIVO"),
        ("vida-hipotecario-2025", "COP", "ACTIVO"),
    ]
    reglas = _filas(base, "SELECT producto_id, version, definicion, hash FROM reglas_prima")
    assert len(reglas) == 2
    for _, _, definicion, hash_ in reglas:
        assert hash_ == hash_canonico(definicion)


def test_reglas_de_prima_inmutables(base: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        _filas(base, "UPDATE reglas_prima SET publicado_por = 'otro'")
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        _filas(base, "DELETE FROM reglas_prima")


def test_restricciones_del_producto(base: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _filas(
            base,
            "INSERT INTO productos (id, version, nombre, ramo, pais, moneda, estado, "
            "vigente_desde, vigente_hasta, coberturas) VALUES ('p', '2026.09.1', 'P', 'R', 'CO', "
            "'COP', 'ACTIVO', '2026-09-01', '2026-01-01', '[]')",
        )


def test_api_sobre_la_base_real(base: str) -> None:
    with TestClient(crear_app(Settings(SOLVENTA_DATABASE_URL=base))) as api:
        producto = api.get("/v1/productos/vida-hipotecario")
        vencido = api.get("/v1/productos/vida-hipotecario-2025").json()
        reglas = api.get(
            "/v1/productos/vida-hipotecario/reglas-prima/vigente",
            params={"fecha": "2026-10-15T15:00:00Z"},
        )
        antes = api.get(
            "/v1/productos/vida-hipotecario/reglas-prima/vigente",
            params={"fecha": "2026-08-31T23:59:59Z"},
        )
        version = api.get("/v1/productos/vida-hipotecario-2025/reglas-prima/2025.01.1")
        inexistente = api.get("/v1/productos/no-existe/reglas-prima/vigente")
    assert producto.status_code == 200 and len(producto.json()["coberturas"]) == 4
    assert vencido["vigenteHasta"] == "2026-09-01T00:00:00Z"
    assert reglas.json()["version"] == "2026.09.1"
    assert reglas.json()["hash"] == hash_canonico(reglas.json()["definicion"])
    assert (antes.status_code, antes.json()["codigo"]) == (404, "REGLAS_NO_VIGENTES")
    assert version.json()["definicion"]["moneda"] == "COP"
    assert inexistente.json()["codigo"] == "PRODUCTO_NO_ENCONTRADO"


def test_catalogo_llena_e_invalida_la_cache(base: str, valkey: str) -> None:
    import redis

    directo = redis.Redis.from_url(valkey)
    directo.set(clave_producto("vida-hipotecario"), b"viejo")
    # Holgura para la primera conexión en la CI: con 50 ms la caché se omitiría en silencio.
    settings = Settings(SOLVENTA_DATABASE_URL=base, CACHE_URL=valkey, cache_timeout_ms=2000)
    with TestClient(crear_app(settings)) as api:
        # Al arrancar invalidó lo que había (el despliegue pudo cambiar la semilla).
        assert directo.get(clave_producto("vida-hipotecario")) is None
        respuesta = api.get("/v1/productos/vida-hipotecario").json()
        reglas = api.get("/v1/productos/vida-hipotecario/reglas-prima/vigente").json()
    guardado = directo.get(clave_producto("vida-hipotecario"))
    assert guardado is not None and json.loads(guardado) == respuesta
    assert 0 < directo.ttl(clave_producto("vida-hipotecario")) <= 60
    vigentes = directo.get(clave_reglas_vigentes("vida-hipotecario"))
    assert vigentes is not None and json.loads(vigentes) == reglas
