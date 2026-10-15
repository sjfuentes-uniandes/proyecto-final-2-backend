import json
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any

import pytest
from catalogo.config import Settings
from catalogo.domain.reglas.errores import VersionExistente
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.domain.reglas.modelo import ModeloPublicado
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


@pytest.fixture
def api(repositorio: RepositorioMemoria, reloj: RelojFijo) -> Iterator[TestClient]:
    """App con el repositorio en memoria y el reloj fijo, con su lifespan arrancado."""
    with TestClient(crear_app(Settings(), repositorio=repositorio, reloj=reloj)) as cliente:
        yield cliente
