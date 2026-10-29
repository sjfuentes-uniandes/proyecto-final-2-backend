"""RecaudosPg con conexiones falsas (sin base). La base real la prueban las de módulo."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Self

import pytest
from dinero.adapters.outbound import recaudos_pg
from dinero.adapters.outbound.recaudos_pg import (
    COLUMNAS,
    RecaudosPg,
    fila_recaudo,
    recaudo_de,
)
from dinero.domain.recaudo.modelos import EstadoRecaudo, Recaudo

pytestmark = pytest.mark.anyio
DESPUES = datetime(2026, 10, 28, 15, 0, 3, tzinfo=UTC)
Fabrica = Callable[..., Recaudo]


class Cursor:
    def __init__(self, filas: list[Any], rowcount: int) -> None:
        self.filas = list(filas)
        self.rowcount = rowcount
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any = None) -> Self:
        self.sentencias.append((sql, parametros))
        return self

    async def fetchone(self) -> Any:
        return self.filas.pop(0) if self.filas else None


class Conexion:
    def __init__(self, filas: list[Any], rowcount: int = 1) -> None:
        self.cursor_ = Cursor(filas, rowcount)
        self.transacciones = 0

    def cursor(self, row_factory: Any = None) -> Cursor:
        return self.cursor_

    async def execute(self, sql: str, parametros: Any = None) -> Cursor:
        return await self.cursor_.execute(sql, parametros)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.transacciones += 1
        yield

    @property
    def sql(self) -> list[str]:
        return [s for s, _ in self.cursor_.sentencias]


class Pool:
    def __init__(self, conexion: Conexion) -> None:
        self.conexion = conexion
        self.prestamos = 0

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Conexion]:
        self.prestamos += 1
        yield self.conexion


@dataclass(frozen=True)
class Transaccion:
    """Como `TransaccionInboxPg` de solventa_common: expone la conexión de la transacción."""

    conexion: Any


def _repo(filas: list[Any], rowcount: int = 1) -> tuple[RecaudosPg, Pool]:
    pool = Pool(Conexion(filas, rowcount))
    return RecaudosPg(pool), pool  # type: ignore[arg-type]


def test_la_fila_vuelve_igual(pendiente: Fabrica) -> None:
    for recaudo in (
        pendiente(),
        pendiente().registrar_incierto("TIEMPO_AGOTADO", DESPUES),
        pendiente().confirmar("TX-SINT-C01", DESPUES),
        pendiente().rechazar("FONDOS_INSUFICIENTES", DESPUES, transaccion_id="TX-R"),
    ):
        fila = fila_recaudo(recaudo)
        assert list(fila) == list(COLUMNAS)
        assert fila["estado"] == recaudo.estado.value and fila["moneda"] == "COP"
        assert isinstance(fila["monto"], Decimal)
        assert recaudo_de(fila) == recaudo


async def test_crear_pendiente_nuevo_en_su_propia_transaccion(pendiente: Fabrica) -> None:
    recaudo = pendiente()
    repo, pool = _repo([(recaudo.recaudo_id,)])
    creacion = await repo.crear_pendiente(recaudo)
    assert (creacion.recaudo, creacion.nuevo) == (recaudo, True)
    [(insertar, parametros)] = pool.conexion.cursor_.sentencias
    assert insertar.startswith("INSERT INTO recaudos")
    assert "ON CONFLICT (clave_recaudo) DO NOTHING RETURNING recaudo_id" in insertar
    assert list(parametros) == [fila_recaudo(recaudo)[c] for c in COLUMNAS]
    assert (pool.prestamos, pool.conexion.transacciones) == (1, 1)


async def test_crear_pendiente_repetido_devuelve_el_existente(pendiente: Fabrica) -> None:
    existente = pendiente()
    otro = pendiente(clave_recaudo=existente.clave_recaudo)
    repo, pool = _repo([None, fila_recaudo(existente)])
    creacion = await repo.crear_pendiente(otro)
    assert (creacion.recaudo, creacion.nuevo) == (existente, False)
    insertar, leer = pool.conexion.sql
    assert "ON CONFLICT (clave_recaudo) DO NOTHING" in insertar
    assert leer.endswith("WHERE clave_recaudo = %s")
    assert pool.conexion.cursor_.sentencias[1][1] == (existente.clave_recaudo,)
    assert pool.conexion.transacciones == 1  # insertar y releer, en la misma transacción


async def test_crear_pendiente_sin_fila_tras_el_conflicto_es_un_error(pendiente: Fabrica) -> None:
    # El disparador impide borrar: no debería ocurrir nunca, pero no se inventa un resultado.
    repo, _ = _repo([None, None])
    with pytest.raises(RuntimeError, match="clave_recaudo"):
        await repo.crear_pendiente(pendiente())


async def test_crear_pendiente_solo_acepta_un_movimiento_pendiente(pendiente: Fabrica) -> None:
    repo, pool = _repo([])
    with pytest.raises(ValueError, match="PENDIENTE"):
        await repo.crear_pendiente(pendiente().confirmar("TX-SINT-C01", DESPUES))
    assert pool.conexion.sql == []


async def test_por_clave(pendiente: Fabrica) -> None:
    recaudo = pendiente()
    repo, pool = _repo([fila_recaudo(recaudo)])
    assert await repo.por_clave(recaudo.clave_recaudo) == recaudo
    assert await repo.por_clave(recaudo.clave_recaudo) is None
    assert pool.conexion.sql[0].endswith("FROM recaudos WHERE clave_recaudo = %s")


@pytest.mark.parametrize("filas_cambiadas", [1, 0])
async def test_registrar_resultado_con_la_guarda_de_pendiente(
    pendiente: Fabrica, filas_cambiadas: int
) -> None:
    confirmado = pendiente().confirmar("TX-SINT-C01", DESPUES)
    repo, pool = _repo([], rowcount=filas_cambiadas)
    assert await repo.registrar_resultado(confirmado) == filas_cambiadas
    [(actualizar, parametros)] = pool.conexion.cursor_.sentencias
    assert actualizar.startswith("UPDATE recaudos SET estado = %s")
    assert actualizar.endswith("WHERE recaudo_id = %s AND estado = 'PENDIENTE'")
    # Solo lo que cambia una transición; lo que trae el evento no se reescribe.
    for inmutable in ("clave_recaudo", "monto", "moneda", "pagador_ref", "creado_en"):
        assert inmutable not in actualizar
    assert parametros == (
        "CONFIRMADO",
        "TX-SINT-C01",
        None,
        None,
        DESPUES,
        DESPUES,
        confirmado.recaudo_id,
    )


async def test_registrar_un_incierto(pendiente: Fabrica) -> None:
    incierto = pendiente().registrar_incierto("TIEMPO_AGOTADO", DESPUES)
    repo, pool = _repo([])
    await repo.registrar_resultado(incierto)
    _, parametros = pool.conexion.cursor_.sentencias[0]
    assert parametros[:6] == ("PENDIENTE", None, None, "TIEMPO_AGOTADO", DESPUES, None)


async def test_dentro_de_una_transaccion_recibida_no_abre_otra(pendiente: Fabrica) -> None:
    """16.10: el manejador del inbox escribe con la conexión de su transacción (`tx.conexion`)."""
    recaudo = pendiente()
    repo, pool = _repo([])
    externa = Conexion([(recaudo.recaudo_id,), fila_recaudo(recaudo)])
    tx = Transaccion(externa)
    assert (await repo.crear_pendiente(recaudo, tx)).nuevo is True
    assert await repo.por_clave(recaudo.clave_recaudo, tx) == recaudo
    await repo.registrar_resultado(recaudo.confirmar("TX", DESPUES + timedelta(seconds=1)), tx)
    assert len(externa.sql) == 3
    assert (pool.prestamos, externa.transacciones) == (0, 0)  # ni pool ni transacción propia


class ConexionSincrona:
    def __init__(self, falla_en: str | None = None) -> None:
        self.sql: list[str] = []
        self.falla_en = falla_en

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.sql.append(sql)
        if self.falla_en and self.falla_en in sql:
            raise RuntimeError("falla de la migración")


@pytest.mark.parametrize("falla", [False, True])
def test_migrar_aplica_en_orden_con_candado_y_lo_suelta(
    monkeypatch: pytest.MonkeyPatch, falla: bool
) -> None:
    conexion = ConexionSincrona("CREATE TABLE IF NOT EXISTS recaudos" if falla else None)
    conexiones: list[tuple[str, dict[str, Any]]] = []

    def conectar(url: str, **opciones: Any) -> ConexionSincrona:
        conexiones.append((url, opciones))
        return conexion

    monkeypatch.setattr(recaudos_pg.psycopg, "connect", conectar)
    if falla:
        with pytest.raises(RuntimeError):
            recaudos_pg.migrar("postgresql://base")
    else:
        recaudos_pg.migrar("postgresql://base")
    assert conexiones == [("postgresql://base", {"autocommit": True})]
    candado, *migraciones, liberar = conexion.sql
    assert "pg_advisory_lock(hashtext('dinero.migraciones'))" in candado
    assert "pg_advisory_unlock(hashtext('dinero.migraciones'))" in liberar
    # Solo las de adelante (las de down/ no), y la 001 primero.
    assert len(migraciones) == 1
    assert "CREATE TABLE IF NOT EXISTS recaudos" in migraciones[0]
    assert "DROP TABLE" not in migraciones[0]


def test_recaudo_de_convierte_estado_y_moneda(pendiente: Fabrica) -> None:
    fila = fila_recaudo(pendiente())
    leido = recaudo_de(fila)
    assert leido.estado is EstadoRecaudo.PENDIENTE
    assert leido.moneda.value == "COP"
