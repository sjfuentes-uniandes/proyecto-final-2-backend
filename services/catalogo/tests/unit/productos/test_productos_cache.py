"""Cache-Aside de catalogo (HU-W03): llena al fallar la lectura, respeta la ventana de las reglas
vigentes, ignora valores ajenos al contrato e invalida al arrancar."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from catalogo.adapters.outbound.productos_cache import (
    TTL_VERSION_S,
    ProductosConCache,
    clave_producto,
    clave_reglas_version,
    clave_reglas_vigentes,
)
from catalogo.config import Settings
from catalogo.main import crear_app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)


class CacheMemoria:
    def __init__(self) -> None:
        self.valores: dict[str, bytes] = {}
        self.ttl: dict[str, int] = {}
        self.lecturas: list[str] = []
        self.borradas: list[str] = []
        self.cerrada = False

    async def leer(self, clave: str) -> bytes | None:
        self.lecturas.append(clave)
        return self.valores.get(clave)

    async def escribir(self, clave: str, valor: bytes, ttl_s: int) -> None:
        self.valores[clave] = valor
        self.ttl[clave] = ttl_s

    async def borrar(self, *claves: str) -> None:
        self.borradas.extend(claves)
        for clave in claves:
            self.valores.pop(clave, None)

    async def cerrar(self) -> None:
        self.cerrada = True


class Contador:
    """Envuelve el repositorio en memoria y cuenta las lecturas a la "base"."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self.llamadas: list[str] = []

    async def producto(self, producto_id: str) -> Any:
        self.llamadas.append("producto")
        return await self.repo.producto(producto_id)

    async def reglas_vigentes(self, producto_id: str, en: datetime) -> Any:
        self.llamadas.append("vigentes")
        return await self.repo.reglas_vigentes(producto_id, en)

    async def reglas_por_version(self, producto_id: str, version: str) -> Any:
        self.llamadas.append("version")
        return await self.repo.reglas_por_version(producto_id, version)


@pytest.fixture
def cache() -> CacheMemoria:
    return CacheMemoria()


@pytest.fixture
def base(productos_repo: Any) -> Contador:
    return Contador(productos_repo)


@pytest.fixture
def con_cache(base: Contador, cache: CacheMemoria, reloj: Any) -> ProductosConCache:
    return ProductosConCache(base, cache, reloj, ttl_s=60)


async def test_producto_se_guarda_y_luego_se_lee_de_la_cache(
    con_cache: ProductosConCache, base: Contador, cache: CacheMemoria
) -> None:
    primero = await con_cache.producto("vida-hipotecario")
    segundo = await con_cache.producto("vida-hipotecario")
    assert primero == segundo
    assert base.llamadas == ["producto"]
    guardado = json.loads(cache.valores[clave_producto("vida-hipotecario")])
    assert guardado["id"] == "vida-hipotecario" and guardado["vigenteDesde"].endswith("Z")
    assert cache.ttl[clave_producto("vida-hipotecario")] == 60


async def test_producto_inexistente_no_se_guarda(
    con_cache: ProductosConCache, cache: CacheMemoria
) -> None:
    assert await con_cache.producto("no-existe") is None
    assert cache.valores == {}


async def test_valor_ajeno_al_contrato_se_reemplaza(
    con_cache: ProductosConCache, base: Contador, cache: CacheMemoria
) -> None:
    cache.valores[clave_producto("vida-hipotecario")] = b'{"id": 1}'
    cache.valores[clave_reglas_vigentes("vida-hipotecario")] = b"no es json"
    assert (await con_cache.producto("vida-hipotecario")) is not None
    assert (await con_cache.reglas_vigentes("vida-hipotecario", AHORA)) is not None
    assert base.llamadas == ["producto", "vigentes"]


async def test_reglas_vigentes_dentro_de_la_ventana(
    con_cache: ProductosConCache, base: Contador, cache: CacheMemoria
) -> None:
    primera = await con_cache.reglas_vigentes("vida-hipotecario", AHORA)
    segunda = await con_cache.reglas_vigentes("vida-hipotecario", AHORA + timedelta(seconds=30))
    assert primera is not None and segunda is not None
    assert (primera.version, primera.hash) == (segunda.version, segunda.hash)
    assert primera.definicion == segunda.definicion
    assert base.llamadas == ["vigentes"]


async def test_fecha_historica_va_a_la_base_y_no_toca_la_cache(
    con_cache: ProductosConCache, base: Contador, cache: CacheMemoria
) -> None:
    historica = AHORA - timedelta(days=400)
    assert await con_cache.reglas_vigentes("vida-hipotecario-2025", historica) is not None
    assert cache.lecturas == [] and cache.valores == {}
    assert base.llamadas == ["vigentes"]


async def test_reglas_guardadas_posteriores_a_la_fecha_no_se_usan(
    con_cache: ProductosConCache, base: Contador, cache: CacheMemoria, reloj: Any
) -> None:
    await con_cache.reglas_vigentes("vida-hipotecario", AHORA)
    reloj.instante = datetime(2026, 9, 1, tzinfo=UTC)
    antes = datetime(2026, 8, 31, 23, 59, 30, tzinfo=UTC)
    assert await con_cache.reglas_vigentes("vida-hipotecario", antes) is None
    assert base.llamadas == ["vigentes", "vigentes"]


async def test_reglas_por_version_con_ttl_largo(
    con_cache: ProductosConCache, base: Contador, cache: CacheMemoria
) -> None:
    clave = clave_reglas_version("vida-hipotecario", "2026.09.1")
    assert await con_cache.reglas_por_version("vida-hipotecario", "2026.09.1") is not None
    assert await con_cache.reglas_por_version("vida-hipotecario", "2026.09.1") is not None
    assert await con_cache.reglas_por_version("vida-hipotecario", "2030.01.1") is None
    assert cache.ttl[clave] == TTL_VERSION_S
    assert base.llamadas == ["version", "version"]


async def test_ttl_cero_no_usa_la_cache(base: Contador, cache: CacheMemoria, reloj: Any) -> None:
    sin_cache = ProductosConCache(base, cache, reloj, ttl_s=0)
    await sin_cache.producto("vida-hipotecario")
    await sin_cache.reglas_vigentes("vida-hipotecario", AHORA)
    await sin_cache.reglas_por_version("vida-hipotecario", "2026.09.1")
    assert cache.lecturas == [] and cache.valores == {}


async def test_invalidar_borra_producto_y_vigentes(
    con_cache: ProductosConCache, cache: CacheMemoria
) -> None:
    await con_cache.invalidar(["vida-hipotecario"])
    assert cache.borradas == [
        clave_producto("vida-hipotecario"),
        clave_reglas_vigentes("vida-hipotecario"),
    ]


def test_el_arranque_con_base_invalida_la_cache_y_la_cierra(
    monkeypatch: pytest.MonkeyPatch, cache: CacheMemoria
) -> None:
    """El lifespan real (sin base de verdad): migra, abre el pool, invalida y cierra la caché."""
    from catalogo.adapters.outbound import modelos_pg

    abiertos: list[str] = []

    class PoolFalso:
        def __init__(self, url: str, **_: Any) -> None:
            abiertos.append(url)

        async def open(self) -> None:
            return None

        async def close(self) -> None:
            abiertos.append("cerrado")

    monkeypatch.setattr(modelos_pg, "migrar", lambda url: abiertos.append("migrado"))
    monkeypatch.setattr("psycopg_pool.AsyncConnectionPool", PoolFalso)
    settings = Settings(SOLVENTA_DATABASE_URL="postgresql://u@h/catalogo")
    with TestClient(crear_app(settings, cache=cache)):
        assert abiertos[:2] == ["migrado", "postgresql://u@h/catalogo"]
    assert clave_producto("vida-hipotecario-2025") in cache.borradas
    assert cache.cerrada and abiertos[-1] == "cerrado"
