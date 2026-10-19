"""Semilla de QA del modelo de riesgo (DEF-02, D-04 a) con una conexión falsa; el SQL real lo
cubre tests/module/test_modelos_riesgo.py."""

from typing import Any, Self

import psycopg_pool
import pytest
from catalogo.adapters.inbound.http.esquema_modelo import DefinicionModelo, a_dict
from catalogo.adapters.outbound import modelos_pg
from catalogo.adapters.outbound.modelos_pg import sembrar_qa, semilla_qa
from catalogo.config import Settings
from catalogo.domain.reglas.errores import DefinicionInvalida
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.domain.reglas.validacion import validar_definicion
from catalogo.main import crear_app
from fastapi.testclient import TestClient
from pydantic import ValidationError

NOMBRE = "modelo_2026_10_2_qa"


class ConexionSincrona:
    def __init__(self, filas: int = 1) -> None:
        self.rowcount = filas
        self.sentencias: list[tuple[str, Any]] = []
        self.argumentos: dict[str, Any] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, parametros: Any = None) -> Self:
        self.sentencias.append((sql, parametros))
        return self


def _conectar(monkeypatch: pytest.MonkeyPatch, conexion: ConexionSincrona) -> None:
    def conectar(url: str, **argumentos: Any) -> ConexionSincrona:
        conexion.argumentos = {"url": url, **argumentos}
        return conexion

    monkeypatch.setattr(modelos_pg.psycopg, "connect", conectar)


def test_la_version_de_qa_solo_sube_el_umbral_de_bajo(definicion_candidata: dict[str, Any]) -> None:
    """04 §B.6.1 con `niveles[0].desde = "80"`: C01 (75,90) pasa de BAJO a MEDIO."""
    version, definicion = semilla_qa(NOMBRE)

    assert version == "2026.10.2"
    assert definicion["niveles"][0] == {"desde": "80", "nivel": "BAJO"}
    definicion_candidata["niveles"][0]["desde"] = "80"
    assert definicion == definicion_candidata


def test_la_version_de_qa_pasa_el_esquema_y_la_validacion() -> None:
    _, definicion = semilla_qa(NOMBRE)

    assert a_dict(DefinicionModelo.model_validate(definicion)) == definicion
    assert validar_definicion(definicion) == ()


@pytest.mark.parametrize("filas", [1, 0], ids=["insertada", "ya-existia"])
def test_sembrar_qa_inserta_vigente_desde_ahora_sin_pisar(
    monkeypatch: pytest.MonkeyPatch, filas: int
) -> None:
    conexion = ConexionSincrona(filas)
    _conectar(monkeypatch, conexion)

    assert sembrar_qa("postgresql://u@h/catalogo", NOMBRE) is (filas == 1)

    assert conexion.argumentos == {"url": "postgresql://u@h/catalogo", "autocommit": True}
    ((sql, parametros),) = conexion.sentencias
    assert "now()" in sql and "ON CONFLICT (version) DO NOTHING" in sql
    _, definicion = semilla_qa(NOMBRE)
    version, producto, jsonb, hash_, actor = parametros
    assert (version, producto, actor) == ("2026.10.2", "vida-hipotecario", "semilla-qa")
    assert jsonb.obj == definicion
    assert hash_ == hash_canonico(definicion)


def test_una_definicion_de_qa_invalida_no_se_inserta(
    monkeypatch: pytest.MonkeyPatch, definicion_candidata: dict[str, Any]
) -> None:
    definicion_candidata["factores"][0]["peso"] = "0.99"
    monkeypatch.setattr(modelos_pg, "semilla_qa", lambda _: ("2026.10.9", definicion_candidata))
    conexion = ConexionSincrona()
    _conectar(monkeypatch, conexion)

    with pytest.raises(DefinicionInvalida) as error:
        sembrar_qa("postgresql://u@h/catalogo", "invalida")

    assert "PESOS_NO_SUMAN_1" in error.value.errores
    assert conexion.sentencias == []


def test_sin_la_variable_no_hay_semilla_de_qa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CATALOGO_SEMILLA_QA", raising=False)

    assert Settings().semilla_qa is None


@pytest.mark.parametrize("nombre", ["CATALOGO_SEMILLA_QA", "SOLVENTA_CATALOGO_SEMILLA_QA"])
def test_la_variable_de_la_infra_nombra_el_archivo(
    monkeypatch: pytest.MonkeyPatch, nombre: str
) -> None:
    monkeypatch.setenv(nombre, NOMBRE)

    assert Settings().semilla_qa == NOMBRE


@pytest.mark.parametrize("valor", ["../modelo_2026_10_1", "qa/otro", "Modelo.json"])
def test_el_nombre_no_puede_salir_de_la_carpeta_qa(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("CATALOGO_SEMILLA_QA", valor)

    with pytest.raises(ValidationError):
        Settings()


class PoolFalso:
    def __init__(self, url: str, **_: Any) -> None:
        self.url = url

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.parametrize("semilla", [NOMBRE, None, ""])
def test_el_arranque_publica_la_semilla_de_qa_despues_de_migrar(
    monkeypatch: pytest.MonkeyPatch, semilla: str | None
) -> None:
    pasos: list[tuple[str, ...]] = []
    monkeypatch.setattr(modelos_pg, "migrar", lambda _url: pasos.append(("migrar",)))
    monkeypatch.setattr(
        modelos_pg, "sembrar_qa", lambda _url, nombre: pasos.append(("sembrar_qa", nombre))
    )
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)
    settings = Settings(DB_HOST="rds.interno", CATALOGO_SEMILLA_QA=semilla)

    with TestClient(crear_app(settings)):
        pass

    esperado = [("migrar",), ("sembrar_qa", NOMBRE)] if semilla else [("migrar",)]
    assert pasos == esperado
