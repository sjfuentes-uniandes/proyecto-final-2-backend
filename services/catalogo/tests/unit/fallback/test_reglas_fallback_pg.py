"""ReglasFallbackPg, la siembra de reglas de fallback y ConsultarReglasFallback (HU-W06), con
conexiones falsas (sin base)."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from catalogo.adapters.outbound import reglas_fallback_pg
from catalogo.adapters.outbound.modelos_pg import SEMILLA_PRODUCTO, definicion_semilla
from catalogo.adapters.outbound.reglas_fallback_pg import ReglasFallbackPg, sembrar, semillas
from catalogo.application.fallback.consultar_reglas import ConsultarReglasFallback
from catalogo.domain.fallback.errores import ReglasFallbackInvalidas, ReglasFallbackNoVigentes
from catalogo.domain.productos.errores import ProductoNoEncontrado
from catalogo.domain.reglas.hash import hash_canonico
from pydantic import ValidationError

pytestmark = pytest.mark.anyio
VIGENTE = datetime(2026, 10, 1, tzinfo=UTC)
MODELOS = {SEMILLA_PRODUCTO: definicion_semilla()}


class Cursor:
    def __init__(self, fila: dict[str, Any] | None) -> None:
        self.fila = fila
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any) -> None:
        self.sentencias.append((sql, parametros))

    async def fetchone(self) -> dict[str, Any] | None:
        return self.fila


class Conexion:
    def __init__(self, fila: dict[str, Any] | None) -> None:
        self.cursor_ = Cursor(fila)

    def cursor(self, row_factory: Any) -> Cursor:
        return self.cursor_


class Pool:
    def __init__(self, conexion: Conexion) -> None:
        self.conexion = conexion

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Conexion]:
        yield self.conexion


class ConexionSincrona:
    def __init__(self) -> None:
        self.sentencias: list[tuple[str, Any]] = []

    def execute(self, sql: str, parametros: Any = None) -> None:
        self.sentencias.append((sql, parametros))


def test_sembrar_inserta_la_version_sin_pisar_la_existente() -> None:
    conexion = ConexionSincrona()
    sembrar(conexion, MODELOS)  # type: ignore[arg-type]
    [(sql, parametros)] = conexion.sentencias
    assert "INSERT INTO reglas_fallback" in sql
    assert "ON CONFLICT (producto_id, version) DO NOTHING" in sql
    producto, version, desde, hasta, definicion, hash_, publicado_por = parametros
    assert (producto, version, desde, hasta) == ("vida-hipotecario", "2026.10.1", VIGENTE, None)
    assert hash_ == hash_canonico(definicion.obj)
    assert publicado_por == "migracion"


def _semillas_en(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, definicion: Any) -> None:
    entrada = {
        "version": "2026.10.9",
        "vigenteDesde": "2026-10-01T00:00:00Z",
        "vigenteHasta": "2026-11-01T00:00:00Z",
        "archivo": "reglas.json",
    }
    productos = [
        {"id": "vida-hipotecario", "reglasFallback": [entrada]},
        {"id": "sin-reglas-de-fallback"},
    ]
    (tmp_path / "productos.json").write_text(json.dumps(productos), encoding="utf-8")
    (tmp_path / "reglas.json").write_text(json.dumps(definicion), encoding="utf-8")
    monkeypatch.setattr(reglas_fallback_pg, "_SEMILLAS", tmp_path)


def test_una_semilla_que_degrada_una_obligatoria_impide_arrancar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definicion = dict(semillas(MODELOS)[0][4])
    definicion["reglas"] = [dict(r, accion="DEGRADAR") for r in definicion["reglas"]]
    _semillas_en(tmp_path, monkeypatch, definicion)
    with pytest.raises(ReglasFallbackInvalidas) as error:
        semillas(MODELOS)
    assert error.value.errores == (
        "OBLIGATORIA_NO_DEGRADABLE:FB-001",
        "OBLIGATORIA_NO_DEGRADABLE:FB-002",
    )


def test_una_semilla_fuera_del_esquema_impide_arrancar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _semillas_en(tmp_path, monkeypatch, dict(semillas(MODELOS)[0][4], extra=True))
    with pytest.raises(ValidationError):
        semillas(MODELOS)


def test_los_productos_sin_reglas_de_fallback_se_omiten_y_el_vencimiento_se_lee(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _semillas_en(tmp_path, monkeypatch, semillas(MODELOS)[0][4])
    assert [(p, v, h) for p, v, _, h, _ in semillas(MODELOS)] == [
        ("vida-hipotecario", "2026.10.9", datetime(2026, 11, 1, tzinfo=UTC))
    ]


def _fila() -> dict[str, Any]:
    definicion = semillas(MODELOS)[0][4]
    return {
        "producto_id": "vida-hipotecario",
        "version": "2026.10.1",
        "vigente_desde": VIGENTE,
        "vigente_hasta": None,
        "definicion": definicion,
        "hash": hash_canonico(definicion),
        "publicado_por": "migracion",
    }


async def test_vigentes_y_por_version_leen_una_fila() -> None:
    conexion = Conexion(_fila())
    repo = ReglasFallbackPg(Pool(conexion))  # type: ignore[arg-type]
    vigentes = await repo.vigentes("vida-hipotecario", VIGENTE)
    por_version = await repo.por_version("vida-hipotecario", "2026.10.1")
    assert vigentes == por_version
    assert vigentes is not None and vigentes.version == "2026.10.1"
    (sql_vigentes, p1), (sql_version, p2) = conexion.cursor_.sentencias
    assert "vigente_desde <= %s" in sql_vigentes and "%s < vigente_hasta" in sql_vigentes
    assert p1 == ("vida-hipotecario", VIGENTE, VIGENTE)
    assert "version = %s" in sql_version and p2 == ("vida-hipotecario", "2026.10.1")


async def test_sin_fila_devuelve_none() -> None:
    repo = ReglasFallbackPg(Pool(Conexion(None)))  # type: ignore[arg-type]
    assert await repo.vigentes("vida-hipotecario", VIGENTE) is None
    assert await repo.por_version("vida-hipotecario", "2030.01.1") is None


async def test_el_caso_de_uso_usa_el_reloj_y_distingue_el_producto_inexistente(
    reglas_fallback_repo: Any, productos_repo: Any, reloj: Any
) -> None:
    caso = ConsultarReglasFallback(reglas_fallback_repo, productos_repo, reloj)
    assert (await caso.vigentes("vida-hipotecario", None)).version == "2026.10.1"
    with pytest.raises(ProductoNoEncontrado):
        await caso.vigentes("no-existe", None)
    with pytest.raises(ReglasFallbackNoVigentes):
        await caso.vigentes("vida-hipotecario-2025", None)
    with pytest.raises(ReglasFallbackNoVigentes):
        await caso.por_version("vida-hipotecario", "2030.01.1")
    assert (await caso.por_version("vida-hipotecario", "2026.10.1")).publicado_por == "migracion"
