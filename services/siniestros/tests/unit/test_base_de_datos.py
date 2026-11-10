"""`migrar()` con una conexión simulada: orden, bloqueo entre réplicas y liberación (HU-W19)."""

from typing import Any, Self

import pytest
from siniestros.adapters.outbound import base_de_datos


class ConexionFalsa:
    def __init__(self, falla_en: str | None = None) -> None:
        self.sentencias: list[str] = []
        self.falla_en = falla_en

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.sentencias.append(sql)
        if self.falla_en and self.falla_en in sql:
            raise RuntimeError("falla simulada")


@pytest.fixture
def conectar(monkeypatch: pytest.MonkeyPatch) -> ConexionFalsa:
    conexion = ConexionFalsa()
    monkeypatch.setattr(
        base_de_datos.psycopg,
        "connect",
        lambda url, **kwargs: _registrar(conexion, url, kwargs),
    )
    return conexion


def _registrar(conexion: ConexionFalsa, url: str, kwargs: dict[str, Any]) -> ConexionFalsa:
    assert url == "postgresql://prueba"
    assert kwargs == {"autocommit": True}
    return conexion


def test_aplica_las_migraciones_entre_el_bloqueo_y_su_liberacion(conectar: ConexionFalsa) -> None:
    base_de_datos.migrar("postgresql://prueba")
    assert conectar.sentencias[0] == base_de_datos._LOCK
    assert conectar.sentencias[-1] == base_de_datos._UNLOCK
    migraciones = conectar.sentencias[1:-1]
    assert len(migraciones) == 2
    assert "CREATE TABLE IF NOT EXISTS siniestros" in migraciones[0]
    assert "CREATE TABLE IF NOT EXISTS decisiones_siniestro" in migraciones[1]


def test_libera_el_bloqueo_aunque_una_migracion_falle(
    conectar: ConexionFalsa,
) -> None:
    conectar.falla_en = "CREATE TABLE IF NOT EXISTS siniestros"
    with pytest.raises(RuntimeError, match="falla simulada"):
        base_de_datos.migrar("postgresql://prueba")
    assert conectar.sentencias[-1] == base_de_datos._UNLOCK


def test_las_reversas_no_se_aplican_al_arrancar(conectar: ConexionFalsa) -> None:
    base_de_datos.migrar("postgresql://prueba")
    assert not any("DROP TABLE" in sql for sql in conectar.sentencias)
