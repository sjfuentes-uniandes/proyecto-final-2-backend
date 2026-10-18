"""Repositorio Postgres con una conexión falsa; el SQL real lo cubre tests/module."""

from datetime import UTC, date, datetime
from typing import Any, Self
from uuid import UUID

import httpx
import psycopg
import pytest
from clientes.adapters.outbound import postgres
from clientes.adapters.outbound.postgres import RepositorioPostgres, migrar
from clientes.config import Settings
from clientes.domain.identidad.modelos import (
    Cliente,
    DatosCliente,
    EstadoIdentidad,
    RegistroYaVinculado,
)
from clientes.main import crear_app
from fastapi.testclient import TestClient

ID = UUID("00000000-0000-4000-8000-000000000001")
REGISTRO = UUID("00000000-0000-4000-8000-0000000000aa")
DATOS = DatosCliente("CC", "1020456789", "Natalia González Ruiz", date(1990, 4, 18))
VERIFICADO_EN = datetime(2026, 10, 15, 15, tzinfo=UTC)
FILA = {
    "id": ID,
    "sub": "sub-1",
    "estado": "VERIFICADO",
    "verificado_en": VERIFICADO_EN,
    "registro_id": REGISTRO,
    "correo": "persona@ejemplo.co",
    "tipo_documento": None,
    "numero_documento": None,
    "nombre_completo": None,
    "fecha_nacimiento": None,
    "proveedor": "KYC",
    "referencia": "kyc-ref-001",
}
CLIENTE = Cliente(
    id=ID,
    sub="sub-1",
    estado=EstadoIdentidad.VERIFICADO,
    verificado_en=VERIFICADO_EN,
    registro_id=REGISTRO,
    correo="persona@ejemplo.co",
    proveedor="KYC",
    referencia="kyc-ref-001",
)


class ConexionFalsa:
    def __init__(self, fila: dict[str, Any] | None) -> None:
        self.fila = fila
        self.error: Exception | None = None
        self.sentencias: list[tuple[str, Any]] = []
        self.argumentos: dict[str, Any] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, parametros: Any = None) -> "ConexionFalsa":
        self.sentencias.append((sql, parametros))
        if self.error is not None:
            raise self.error
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self.fila


def _conectar(monkeypatch: pytest.MonkeyPatch, fila: dict[str, Any] | None) -> ConexionFalsa:
    falsa = ConexionFalsa(fila)

    def conectar(url: str, **argumentos: Any) -> ConexionFalsa:
        falsa.argumentos = {"url": url, **argumentos}
        return falsa

    monkeypatch.setattr(postgres.psycopg, "connect", conectar)
    return falsa


def test_mapea_la_fila_al_cliente(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = _conectar(monkeypatch, FILA)
    repositorio = RepositorioPostgres("postgresql://u@h/clientes")

    assert repositorio.obtener(ID) == CLIENTE
    assert conexion.sentencias[-1] == (postgres._POR_ID, (ID,))
    assert repositorio.por_sub("sub-1") == CLIENTE
    assert conexion.sentencias[-1] == (postgres._POR_SUB, ("sub-1",))
    assert conexion.argumentos["url"] == "postgresql://u@h/clientes"


def test_inexistente_devuelve_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _conectar(monkeypatch, None)
    repositorio = RepositorioPostgres("postgresql://u@h/clientes")

    assert repositorio.obtener(ID) is None
    assert repositorio.por_sub("nadie") is None


def test_asegurar_inserta_sin_pisar_y_lee_el_cliente_del_sub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conexion = _conectar(monkeypatch, FILA)

    assert RepositorioPostgres("postgresql://u@h/clientes").asegurar("sub-1") == CLIENTE
    (crear, parametros_crear), (leer, parametros_leer) = conexion.sentencias
    assert crear == postgres._CREAR and "ON CONFLICT (sub) DO NOTHING" in crear
    assert parametros_crear[1] == "sub-1"
    assert (leer, parametros_leer) == (postgres._POR_SUB, ("sub-1",))


def test_asegurar_falla_si_el_cliente_no_existe_tras_crearlo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conectar(monkeypatch, None)

    with pytest.raises(RuntimeError):
        RepositorioPostgres("postgresql://u@h/clientes").asegurar("sub-1")


def test_por_registro_mapea_y_devuelve_none_si_no_existe(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = _conectar(monkeypatch, FILA)
    repositorio = RepositorioPostgres("postgresql://u@h/clientes")

    assert repositorio.por_registro(REGISTRO) == CLIENTE
    assert conexion.sentencias[-1] == (postgres._POR_REGISTRO, (REGISTRO,))
    conexion.fila = None
    assert repositorio.por_registro(REGISTRO) is None


def test_asegurar_registro_inserta_sin_pisar_y_lee_el_cliente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conexion = _conectar(monkeypatch, FILA)

    assert (
        RepositorioPostgres("postgresql://u@h/clientes").asegurar_registro(
            REGISTRO, "persona@ejemplo.co"
        )
        == CLIENTE
    )
    (crear, parametros), (leer, parametros_leer) = conexion.sentencias
    assert crear == postgres._CREAR_REGISTRO and "ON CONFLICT (registro_id) DO NOTHING" in crear
    assert parametros[1:] == (REGISTRO, "persona@ejemplo.co")
    assert (leer, parametros_leer) == (postgres._POR_REGISTRO, (REGISTRO,))


def test_asegurar_registro_falla_si_el_cliente_no_existe_tras_crearlo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conectar(monkeypatch, None)

    with pytest.raises(RuntimeError):
        RepositorioPostgres("postgresql://u@h/clientes").asegurar_registro(REGISTRO, "a@b.co")


def test_vincular_asocia_el_sub_solo_si_esta_libre_o_es_el_mismo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conexion = _conectar(monkeypatch, FILA)

    assert RepositorioPostgres("postgresql://u@h/clientes").vincular(ID, "sub-1") == CLIENTE
    assert conexion.sentencias == [(postgres._VINCULAR, ("sub-1", ID, "sub-1"))]
    assert "sub IS NULL OR sub = %s" in postgres._VINCULAR


def test_vincular_un_cliente_de_otro_sub_es_registro_ya_vinculado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conectar(monkeypatch, None)

    with pytest.raises(RegistroYaVinculado):
        RepositorioPostgres("postgresql://u@h/clientes").vincular(ID, "sub-2")


def test_vincular_un_sub_que_ya_es_de_otro_cliente_es_registro_ya_vinculado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conexion = _conectar(monkeypatch, FILA)
    conexion.error = psycopg.errors.UniqueViolation()

    with pytest.raises(RegistroYaVinculado):
        RepositorioPostgres("postgresql://u@h/clientes").vincular(ID, "sub-2")


def test_guardar_actualiza_el_estado(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = _conectar(monkeypatch, FILA)

    RepositorioPostgres("postgresql://u@h/clientes").guardar(CLIENTE)

    assert conexion.sentencias == [
        (postgres._ACTUALIZAR, ("VERIFICADO", VERIFICADO_EN, "KYC", "kyc-ref-001", ID))
    ]


def test_asegurar_por_documento_distingue_el_alta_del_reintento(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conexion = _conectar(monkeypatch, FILA)
    repositorio = RepositorioPostgres("postgresql://u@h/clientes")

    cliente, creado = repositorio.asegurar_por_documento(DATOS)

    assert (cliente, creado) == (CLIENTE, True)
    (crear, parametros), (leer, parametros_leer) = conexion.sentencias
    assert crear == postgres._CREAR_DOCUMENTO
    assert "ON CONFLICT (tipo_documento, numero_documento) DO NOTHING RETURNING id" in crear
    assert parametros[1:] == ("CC", "1020456789", "Natalia González Ruiz", date(1990, 4, 18))
    assert (leer, parametros_leer) == (postgres._POR_DOCUMENTO, ("CC", "1020456789"))


def test_asegurar_por_documento_existente_no_es_alta(monkeypatch: pytest.MonkeyPatch) -> None:
    class SinFilaNueva(ConexionFalsa):
        def fetchone(self) -> dict[str, Any] | None:
            # El INSERT ... RETURNING no devuelve fila si el documento ya existía.
            return None if postgres._CREAR_DOCUMENTO == self.sentencias[-1][0] else self.fila

    falsa = SinFilaNueva(FILA)
    monkeypatch.setattr(postgres.psycopg, "connect", lambda url, **_: falsa)

    _, creado = RepositorioPostgres("postgresql://u@h/clientes").asegurar_por_documento(DATOS)

    assert creado is False


def test_asegurar_por_documento_falla_si_el_cliente_no_existe_tras_crearlo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conectar(monkeypatch, None)

    with pytest.raises(RuntimeError):
        RepositorioPostgres("postgresql://u@h/clientes").asegurar_por_documento(DATOS)


def test_mapea_el_documento_del_cliente(monkeypatch: pytest.MonkeyPatch) -> None:
    fila = {
        **FILA,
        "tipo_documento": "CC",
        "numero_documento": "1020456789",
        "nombre_completo": "Natalia González Ruiz",
        "fecha_nacimiento": date(1990, 4, 18),
    }
    _conectar(monkeypatch, fila)

    assert RepositorioPostgres("postgresql://u@h/clientes").obtener(ID).datos == DATOS  # type: ignore[union-attr]


def test_migrar_aplica_la_migracion_del_documento(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = _conectar(monkeypatch, None)

    migrar("postgresql://u@h/clientes")

    sentencias = [sql for sql, _ in conexion.sentencias]
    assert any("cliente_documento_uq" in sql for sql in sentencias)


def test_migrar_aplica_las_migraciones_bajo_un_candado(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = _conectar(monkeypatch, None)

    migrar("postgresql://u@h/clientes")

    sentencias = [sql for sql, _ in conexion.sentencias]
    assert conexion.argumentos["autocommit"] is True
    assert "pg_advisory_lock" in sentencias[0]
    assert "CREATE TABLE IF NOT EXISTS cliente" in sentencias[1]
    assert "pg_advisory_unlock" in sentencias[-1]


def test_sin_repositorio_inyectado_migra_y_usa_postgres(
    monkeypatch: pytest.MonkeyPatch, pools: list[Any]
) -> None:
    migradas: list[str] = []
    monkeypatch.setattr(postgres, "migrar", migradas.append)
    app = crear_app(Settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200)))

    with TestClient(app):
        repositorio = app.state.consultar_cliente._repositorio

    assert migradas == [Settings().database_url]
    assert isinstance(repositorio, RepositorioPostgres)
