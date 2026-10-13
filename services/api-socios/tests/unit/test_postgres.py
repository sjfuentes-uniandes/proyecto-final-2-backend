"""Repositorio Postgres con una conexión psycopg simulada (N1). La base real se prueba en N2."""

from datetime import UTC, datetime
from typing import Any, Self

import pytest
from api_socios.adapters.outbound import postgres
from api_socios.adapters.outbound.postgres import RepositorioPostgres, migrar
from api_socios.domain.socios import CanalVenta, Estado, Permiso, Plan, Socio

CREADO = datetime(2026, 10, 12, 10, tzinfo=UTC)
FILA = {
    "id": "SOC-000007",
    "nombre": "Fintech Caribe S.A.S.",
    "canal": "comercio",
    "plan": "estandar",
    "permisos": ["consultar", "cotizar"],
    "estado": "desactivado",
    "client_id": "cliente7",
    "api_key_id": "key7",
    "creado": CREADO,
    "desactivado": CREADO,
}


class ConexionFalsa:
    def __init__(self, filas: list[dict[str, Any]]) -> None:
        self.filas = filas
        self.sentencias: list[tuple[str, Any]] = []
        self.argumentos: dict[str, Any] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, parametros: Any = None) -> "ConexionFalsa":
        self.sentencias.append((sql, parametros))
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self.filas[0] if self.filas else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.filas


@pytest.fixture
def conexion(monkeypatch: pytest.MonkeyPatch) -> ConexionFalsa:
    falsa = ConexionFalsa([FILA])

    def conectar(url: str, **argumentos: Any) -> ConexionFalsa:
        falsa.argumentos = {"url": url, **argumentos}
        return falsa

    monkeypatch.setattr(postgres.psycopg, "connect", conectar)
    return falsa


def test_mapea_la_fila_al_socio(conexion: ConexionFalsa) -> None:
    repositorio = RepositorioPostgres("postgresql://u@h/api_socios")
    socio = repositorio.obtener("SOC-000007")
    assert socio == Socio(
        id="SOC-000007",
        nombre="Fintech Caribe S.A.S.",
        canal=CanalVenta.COMERCIO,
        plan=Plan.ESTANDAR,
        permisos=frozenset({Permiso.CONSULTAR, Permiso.COTIZAR}),
        client_id="cliente7",
        api_key_id="key7",
        estado=Estado.DESACTIVADO,
        creado=CREADO,
        desactivado=CREADO,
    )
    assert conexion.sentencias[-1] == (postgres._POR_ID, ("SOC-000007",))
    assert repositorio.por_client_id("cliente7") == socio
    assert conexion.sentencias[-1] == (postgres._POR_CLIENT_ID, ("cliente7",))
    assert repositorio.listar() == [socio]
    assert conexion.argumentos["url"] == "postgresql://u@h/api_socios"


def test_sin_filas(conexion: ConexionFalsa) -> None:
    conexion.filas = []
    repositorio = RepositorioPostgres("postgresql://")
    assert repositorio.obtener("SOC-000001") is None
    assert repositorio.por_client_id("x") is None
    assert repositorio.listar() == []
    with pytest.raises(RuntimeError):
        repositorio.siguiente_identificador()


def test_guardar_usa_parametros_y_actualiza_solo_el_estado(conexion: ConexionFalsa) -> None:
    socio = Socio(
        id="SOC-000007",
        nombre="Fintech",
        canal=CanalVenta.OTRO,
        plan=Plan.BASICO,
        permisos=frozenset({Permiso.EMITIR, Permiso.COTIZAR}),
        client_id="c",
        api_key_id="k",
        creado=CREADO,
    )
    RepositorioPostgres("postgresql://").guardar(socio)
    sql, parametros = conexion.sentencias[-1]
    assert sql == postgres._GUARDAR and "%s" in sql
    assert "DO UPDATE SET estado = EXCLUDED.estado, desactivado = EXCLUDED.desactivado" in sql
    assert parametros == (
        "SOC-000007", "Fintech", "otro", "basico", ["cotizar", "emitir"], "activo", "c", "k", CREADO, None,
    )  # fmt: skip


def test_siguiente_identificador_desde_la_secuencia(conexion: ConexionFalsa) -> None:
    conexion.filas = [{"n": 161}]
    assert RepositorioPostgres("postgresql://").siguiente_identificador() == "SOC-000161"
    assert "nextval('socio_secuencia')" in conexion.sentencias[-1][0]


def test_migrar_aplica_los_sql_en_orden_con_candado(conexion: ConexionFalsa) -> None:
    migrar("postgresql://u@h/api_socios")
    sentencias = [sql for sql, _ in conexion.sentencias]
    assert "pg_advisory_lock" in sentencias[0] and "pg_advisory_unlock" in sentencias[-1]
    assert any("CREATE TABLE IF NOT EXISTS socio" in sql for sql in sentencias[1:-1])
    assert conexion.argumentos["autocommit"] is True


def test_migrar_libera_el_candado_si_falla(
    conexion: ConexionFalsa, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = conexion.execute

    def execute(sql: str, parametros: Any = None) -> ConexionFalsa:
        if "CREATE TABLE" in sql:
            raise RuntimeError("sin permisos")
        return original(sql, parametros)

    monkeypatch.setattr(conexion, "execute", execute)
    with pytest.raises(RuntimeError):
        migrar("postgresql://")
    assert "pg_advisory_unlock" in conexion.sentencias[-1][0]
