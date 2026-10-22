"""ReglasSuscripcionPg y la siembra de reglas de suscripción con conexiones falsas (sin base)."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from catalogo.adapters.outbound import reglas_suscripcion_pg
from catalogo.adapters.outbound.reglas_suscripcion_pg import ReglasSuscripcionPg, sembrar, semillas
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.domain.suscripcion.errores import ReglasSuscripcionInvalidas
from pydantic import ValidationError

pytestmark = pytest.mark.anyio
VIGENTE = datetime(2026, 10, 1, tzinfo=UTC)


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
    sembrar(conexion)  # type: ignore[arg-type]
    [(sql, parametros)] = conexion.sentencias
    assert "INSERT INTO reglas_suscripcion" in sql
    assert "ON CONFLICT (producto_id, version) DO NOTHING" in sql
    producto, version, vigente_desde, definicion, hash_, publicado_por = parametros
    assert (producto, version, vigente_desde) == ("vida-hipotecario", "2026.10.1", VIGENTE)
    assert hash_ == hash_canonico(definicion.obj)
    assert publicado_por == "migracion"


def _semillas_en(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, definicion: Any) -> None:
    (tmp_path / "productos.json").write_text(
        json.dumps(
            [
                {
                    "id": "vida-hipotecario",
                    "reglasSuscripcion": [
                        {
                            "version": "2026.10.9",
                            "vigenteDesde": "2026-10-01T00:00:00Z",
                            "archivo": "reglas.json",
                        }
                    ],
                },
                {"id": "sin-reglas-de-suscripcion"},
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "reglas.json").write_text(json.dumps(definicion), encoding="utf-8")
    monkeypatch.setattr(reglas_suscripcion_pg, "_SEMILLAS", tmp_path)


def test_una_semilla_invalida_impide_arrancar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definicion = dict(semillas()[0][3])
    definicion["reglas"] = [dict(r, orden=1) for r in definicion["reglas"]]
    _semillas_en(tmp_path, monkeypatch, definicion)
    with pytest.raises(ReglasSuscripcionInvalidas, match="ORDEN_DUPLICADO:SUS-004") as error:
        semillas()
    assert "ORDEN_DUPLICADO:SUS-019" in error.value.errores


def test_una_semilla_fuera_del_esquema_impide_arrancar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definicion = dict(semillas()[0][3], extra=True)
    _semillas_en(tmp_path, monkeypatch, definicion)
    with pytest.raises(ValidationError):
        semillas()


def test_los_productos_sin_reglas_de_suscripcion_se_omiten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _semillas_en(tmp_path, monkeypatch, semillas()[0][3])
    assert [(p, v) for p, v, _, _ in semillas()] == [("vida-hipotecario", "2026.10.9")]


def _fila() -> dict[str, Any]:
    definicion = semillas()[0][3]
    return {
        "producto_id": "vida-hipotecario",
        "version": "2026.10.1",
        "vigente_desde": VIGENTE,
        "definicion": definicion,
        "hash": hash_canonico(definicion),
        "publicado_por": "migracion",
    }


async def test_vigentes_y_por_version_leen_una_fila() -> None:
    conexion = Conexion(_fila())
    repo = ReglasSuscripcionPg(Pool(conexion))  # type: ignore[arg-type]
    vigentes = await repo.vigentes("vida-hipotecario", VIGENTE)
    por_version = await repo.por_version("vida-hipotecario", "2026.10.1")
    assert vigentes == por_version
    assert vigentes is not None and vigentes.version == "2026.10.1"
    (sql_vigentes, p1), (sql_version, p2) = conexion.cursor_.sentencias
    assert "vigente_desde <= %s" in sql_vigentes and p1 == ("vida-hipotecario", VIGENTE)
    assert "version = %s" in sql_version and p2 == ("vida-hipotecario", "2026.10.1")


async def test_sin_fila_devuelve_none() -> None:
    repo = ReglasSuscripcionPg(Pool(Conexion(None)))  # type: ignore[arg-type]
    assert await repo.vigentes("vida-hipotecario", VIGENTE) is None
    assert await repo.por_version("vida-hipotecario", "2030.01.1") is None
