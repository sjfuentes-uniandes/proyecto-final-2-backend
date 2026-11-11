"""`RegistroPg` y `migrar` con conexiones falsas (sin base): las sentencias y la traducción de
fallos. El comportamiento contra PostgreSQL real está en `tests/module`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Self
from uuid import UUID

import psycopg
import pytest
from adaptador_telemetria.adapters.outbound import base_de_datos
from adaptador_telemetria.adapters.outbound.recibidos_pg import RegistroPg
from adaptador_telemetria.application.telemetria.puertos import DependenciaNoDisponible
from adaptador_telemetria.domain.telemetria.modelos import EventoCanonico

pytestmark = pytest.mark.anyio
RECIBIDO = datetime(2026, 11, 10, 14, 0, 5, tzinfo=UTC)
EVENTO = EventoCanonico(
    fuente="IDEAM-SIM",
    id_externo="IDEAM-2026-1110-0001",
    evento_origen="EVT-2026-0913",
    tipo_evento="LLUVIA_ACUMULADA",
    valor_observado="135",
    unidad="mm",
    zona="11001",
    observado_en=datetime(2026, 11, 10, 14, 0, tzinfo=UTC),
)
CLAVE = "telemetria/2026/11/10/bafc2b26-7c88-5ae3-b5ac-26fdf5427a09.json"


class Cursor:
    def __init__(self, fila: tuple[Any, ...] | None) -> None:
        self.fila = fila

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.fila


class Conexion:
    def __init__(self, filas: list[tuple[Any, ...] | None], error: Exception | None = None) -> None:
        self.filas = list(filas)
        self.sentencias: list[tuple[str, Any]] = []
        self.error = error
        self.transacciones = 0

    async def execute(self, sql: str, parametros: Any = None) -> Cursor:
        if self.error:
            raise self.error
        self.sentencias.append((sql, parametros))
        return Cursor(self.filas.pop(0) if self.filas else None)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.transacciones += 1
        yield


class Pool:
    def __init__(self, conexion: Conexion) -> None:
        self.conexion = conexion

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Conexion]:
        yield self.conexion


async def test_un_evento_nuevo_escribe_el_registro_y_el_outbox_en_una_transaccion() -> None:
    conexion = Conexion([(RECIBIDO,)])
    registrado = await RegistroPg(Pool(conexion)).registrar(EVENTO, RECIBIDO, CLAVE, "corr-1")  # type: ignore[arg-type]
    assert (registrado.nuevo, registrado.recibido_en) == (True, RECIBIDO)
    assert conexion.transacciones == 1
    tablas = [s.split()[2] for s, _ in conexion.sentencias]
    assert tablas == ["eventos_recibidos", "outbox"]
    assert "ON CONFLICT DO NOTHING" in conexion.sentencias[0][0]
    evento_id, fuente, id_externo, origen, ref, correlacion, recibido = conexion.sentencias[0][1]
    assert (fuente, id_externo, origen, ref, correlacion, recibido) == (
        "IDEAM-SIM",
        "IDEAM-2026-1110-0001",
        "EVT-2026-0913",
        CLAVE,
        "corr-1",
        RECIBIDO,
    )
    assert evento_id == UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09")
    # La fila del Outbox: el sobre, con el payload como JSON y sin el cuerpo ni la ubicación.
    parametros = conexion.sentencias[1][1]
    assert parametros[0] == parametros[1] == evento_id
    assert parametros[2:4] == ("EventoParametricoObservado", 1)
    assert parametros[4:6] == (RECIBIDO, "corr-1")
    payload = parametros[6].obj
    assert payload["archivoRef"] == CLAVE and payload["zona"] == "11001"
    assert "ubicacion" not in payload


async def test_un_evento_repetido_devuelve_el_primer_instante_y_no_escribe_otro_outbox() -> None:
    primero = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
    conexion = Conexion([None, (primero,)])
    registrado = await RegistroPg(Pool(conexion)).registrar(EVENTO, RECIBIDO, CLAVE, "corr-2")  # type: ignore[arg-type]
    assert (registrado.nuevo, registrado.recibido_en) == (False, primero)
    assert [s.split()[2] for s, _ in conexion.sentencias if s.startswith("INSERT")] == [
        "eventos_recibidos"
    ]


async def test_recibido_devuelve_el_instante_o_none() -> None:
    assert await RegistroPg(Pool(Conexion([(RECIBIDO,)]))).recibido(UUID(int=1)) == RECIBIDO  # type: ignore[arg-type]
    assert await RegistroPg(Pool(Conexion([None]))).recibido(UUID(int=1)) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("error", [psycopg.OperationalError("password=hunter2"), OSError("red")])
async def test_si_la_base_falla_no_filtra_la_causa(error: Exception, capturar_logs: Any) -> None:
    salida = capturar_logs()
    registro = RegistroPg(Pool(Conexion([], error)))  # type: ignore[arg-type]
    for llamada in (
        registro.recibido(UUID(int=1)),
        registro.registrar(EVENTO, RECIBIDO, CLAVE, "c"),
    ):
        with pytest.raises(DependenciaNoDisponible) as excepcion:
            await llamada
        assert excepcion.value.__cause__ is None and excepcion.value.__suppress_context__
    assert "hunter2" not in salida.getvalue()
    assert "error_type" in salida.getvalue()


# --- Migraciones ---------------------------------------------------------------------------------
class ConexionSincrona:
    def __init__(self) -> None:
        self.sentencias: list[str] = []
        self.argumentos: dict[str, Any] = {}

    def execute(self, sql: str) -> None:
        self.sentencias.append(sql)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_migrar_bloquea_aplica_los_sql_en_orden_y_libera(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = ConexionSincrona()

    def conectar(url: str, **argumentos: Any) -> ConexionSincrona:
        conexion.argumentos = {"url": url, **argumentos}
        return conexion

    monkeypatch.setattr(base_de_datos.psycopg, "connect", conectar)
    base_de_datos.migrar("postgresql://u@h/adaptador_telemetria")
    assert conexion.argumentos == {
        "url": "postgresql://u@h/adaptador_telemetria",
        "autocommit": True,
    }
    assert (
        "pg_advisory_lock(hashtext('adaptador_telemetria.migraciones'))" in conexion.sentencias[0]
    )
    assert "CREATE TABLE IF NOT EXISTS eventos_recibidos" in conexion.sentencias[1]
    assert "pg_advisory_unlock" in conexion.sentencias[-1]
    assert len(conexion.sentencias) == 3


def test_migrar_libera_el_candado_aunque_falle(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = ConexionSincrona()
    ejecutar = conexion.execute

    def falla(sql: str) -> None:
        ejecutar(sql)
        if "CREATE TABLE" in sql:
            raise psycopg.errors.SyntaxError

    conexion.execute = falla  # type: ignore[method-assign]
    monkeypatch.setattr(base_de_datos.psycopg, "connect", lambda url, **kw: conexion)
    with pytest.raises(psycopg.errors.SyntaxError):
        base_de_datos.migrar("postgresql://u@h/x")
    assert "pg_advisory_unlock" in conexion.sentencias[-1]


async def test_sin_base_no_se_lee_ni_se_registra_nada() -> None:
    from adaptador_telemetria.adapters.outbound.recibidos_pg import RegistroSinConfigurar

    sin_base = RegistroSinConfigurar()
    with pytest.raises(DependenciaNoDisponible):
        await sin_base.recibido(UUID(int=1))
    with pytest.raises(DependenciaNoDisponible):
        await sin_base.registrar(EVENTO, RECIBIDO, CLAVE, "c")
