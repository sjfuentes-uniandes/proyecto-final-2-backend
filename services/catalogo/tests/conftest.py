import json
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any

import pytest
from catalogo.adapters.outbound import reglas_suscripcion_pg
from catalogo.adapters.outbound.productos_pg import semillas
from catalogo.config import Settings
from catalogo.domain.productos.producto import Producto, ReglasPrima
from catalogo.domain.reglas.errores import VersionExistente
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.domain.reglas.modelo import ModeloPublicado
from catalogo.domain.suscripcion.reglas import ReglasSuscripcion
from catalogo.main import crear_app
from fastapi.testclient import TestClient

# Reloj de las pruebas (01 §3.5).
AHORA = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def definicion_candidata() -> dict[str, Any]:
    """Modelo candidato 2026.10.1 de 04 §B.6.1 (una copia nueva por prueba)."""
    semilla = files("catalogo.adapters.outbound") / "semillas" / "modelo_2026_10_1.json"
    return json.loads(semilla.read_text(encoding="utf-8"))


class RepositorioMemoria:
    def __init__(self) -> None:
        self.modelos: dict[str, ModeloPublicado] = {}

    async def guardar(self, modelo: ModeloPublicado) -> None:
        if modelo.version in self.modelos:
            raise VersionExistente()
        self.modelos[modelo.version] = modelo

    async def vigente(self, producto_id: str, en: datetime) -> ModeloPublicado | None:
        candidatos = [
            m
            for m in self.modelos.values()
            if m.producto_id == producto_id and m.vigente_desde <= en
        ]
        return max(candidatos, key=lambda m: m.vigente_desde, default=None)

    async def por_version(self, version: str) -> ModeloPublicado | None:
        return self.modelos.get(version)


class RelojFijo:
    def __init__(self, instante: datetime = AHORA) -> None:
        self.instante = instante

    def ahora(self) -> datetime:
        return self.instante


@pytest.fixture
def reloj() -> RelojFijo:
    return RelojFijo()


@pytest.fixture
def modelo_candidato(definicion_candidata: dict[str, Any]) -> ModeloPublicado:
    return ModeloPublicado(
        version="2026.10.1",
        producto_id="vida-hipotecario",
        vigente_desde=datetime(2026, 10, 1, tzinfo=UTC),
        definicion=definicion_candidata,
        hash=hash_canonico(definicion_candidata),
        publicado_por="migracion",
    )


@pytest.fixture
def repositorio(modelo_candidato: ModeloPublicado) -> RepositorioMemoria:
    repo = RepositorioMemoria()
    repo.modelos[modelo_candidato.version] = modelo_candidato
    return repo


class ProductosMemoria:
    """RepositorioProductos con la semilla de HU-W03 (vida-hipotecario y vida-hipotecario-2025)."""

    def __init__(self) -> None:
        self.productos: dict[str, Producto] = {}
        self.reglas: list[ReglasPrima] = []
        for producto, reglas in semillas():
            self.productos[producto.id] = producto
            self.reglas.extend(
                ReglasPrima(producto.id, v, desde, d, hash_canonico(d), "migracion")
                for v, desde, d in reglas
            )

    async def producto(self, producto_id: str) -> Producto | None:
        return self.productos.get(producto_id)

    async def reglas_vigentes(self, producto_id: str, en: datetime) -> ReglasPrima | None:
        candidatas = [
            r for r in self.reglas if r.producto_id == producto_id and r.vigente_desde <= en
        ]
        return max(candidatas, key=lambda r: r.vigente_desde, default=None)

    async def reglas_por_version(self, producto_id: str, version: str) -> ReglasPrima | None:
        return next(
            (r for r in self.reglas if r.producto_id == producto_id and r.version == version),
            None,
        )


@pytest.fixture
def productos_repo() -> ProductosMemoria:
    return ProductosMemoria()


class ReglasSuscripcionMemoria:
    """RepositorioReglasSuscripcion con la semilla de HU-W13 (vida-hipotecario 2026.10.1)."""

    def __init__(self) -> None:
        self.reglas = [
            ReglasSuscripcion(p, v, desde, d, hash_canonico(d), "migracion")
            for p, v, desde, d in reglas_suscripcion_pg.semillas()
        ]

    async def vigentes(self, producto_id: str, en: datetime) -> ReglasSuscripcion | None:
        candidatas = [
            r for r in self.reglas if r.producto_id == producto_id and r.vigente_desde <= en
        ]
        return max(candidatas, key=lambda r: r.vigente_desde, default=None)

    async def por_version(self, producto_id: str, version: str) -> ReglasSuscripcion | None:
        return next(
            (r for r in self.reglas if r.producto_id == producto_id and r.version == version),
            None,
        )


@pytest.fixture
def reglas_suscripcion_repo() -> ReglasSuscripcionMemoria:
    return ReglasSuscripcionMemoria()


@pytest.fixture
def api(
    repositorio: RepositorioMemoria,
    productos_repo: ProductosMemoria,
    reglas_suscripcion_repo: ReglasSuscripcionMemoria,
    reloj: RelojFijo,
) -> Iterator[TestClient]:
    """App con los repositorios en memoria y el reloj fijo, con su lifespan arrancado."""
    app = crear_app(
        Settings(),
        repositorio=repositorio,
        productos_repo=productos_repo,
        reglas_suscripcion_repo=reglas_suscripcion_repo,
        reloj=reloj,
    )
    with TestClient(app) as cliente:
        yield cliente
