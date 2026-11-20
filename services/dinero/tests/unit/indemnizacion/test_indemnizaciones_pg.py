"""IndemnizacionesPg con conexiones falsas (sin base). La base real la prueban las de módulo."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Self
from uuid import uuid4

import pytest
from dinero.adapters.outbound.indemnizaciones_pg import (
    COLUMNAS,
    IndemnizacionesPg,
    fila_indemnizacion,
    indemnizacion_de,
)
from dinero.domain.indemnizacion.modelos import EstadoIndemnizacion

pytestmark = pytest.mark.anyio


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

    async def fetchall(self) -> list[Any]:
        filas, self.filas = self.filas, []
        return filas


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
    conexion: Any


def _repo(filas: list[Any], rowcount: int = 1) -> tuple[IndemnizacionesPg, Pool]:
    pool = Pool(Conexion(filas, rowcount))
    return IndemnizacionesPg(pool), pool  # type: ignore[arg-type]


def _de_la_base(fila: dict[str, Any]) -> dict[str, Any]:
    """Como la devuelve psycopg: `numeric(20,4)` con 4 decimales."""
    return {**fila, "monto": fila["monto"].quantize(Decimal("0.0001"))}


def test_la_fila_vuelve_igual_y_la_escala_de_la_base_se_normaliza(d: Any) -> None:
    for i in (
        d.nueva(),
        d.nueva().registrar_incierto("ADAPTADOR_TIEMPO_AGOTADO", d.DESPUES).reclamar(d.DESPUES),
        d.nueva().confirmar("TRX-1", d.DESPUES),
        d.nueva().rechazar("MOTIVO", d.DESPUES, transaccion_id="TRX-R"),
    ):
        fila = fila_indemnizacion(i)
        assert list(fila) == list(COLUMNAS)
        assert isinstance(fila["monto"], Decimal)
        leida = indemnizacion_de(_de_la_base(fila))
        assert leida == i and leida.monto == Decimal("2500000.00")


async def test_registrar_pendiente_nuevo(d: Any) -> None:
    i = d.nueva()
    repo, pool = _repo([{**_de_la_base(fila_indemnizacion(i)), "nuevo": True}])
    registro = await repo.registrar_pendiente(i, Transaccion(pool.conexion))
    assert (registro.indemnizacion, registro.nuevo) == (i, True)
    [(insertar, parametros)] = pool.conexion.cursor_.sentencias
    assert insertar.startswith("INSERT INTO indemnizaciones")
    assert "ON CONFLICT (siniestro_id, tipo) DO UPDATE" in insertar
    assert "SET duplicados_descartados = indemnizaciones.duplicados_descartados + 1" in insertar
    assert "(xmax = 0) AS nuevo" in insertar
    # El conflicto no toca nada más que el contador.
    assert "estado =" not in insertar and "monto =" not in insertar
    assert list(parametros) == [fila_indemnizacion(i)[c] for c in COLUMNAS]
    assert (pool.prestamos, pool.conexion.transacciones) == (0, 0)  # usó la transacción del inbox


async def test_registrar_pendiente_repetido_devuelve_la_existente_con_el_duplicado(d: Any) -> None:
    existente = d.nueva().registrar_duplicado()
    repo, pool = _repo([{**_de_la_base(fila_indemnizacion(existente)), "nuevo": False}])
    registro = await repo.registrar_pendiente(d.nueva(siniestro_id=existente.siniestro_id))
    assert (registro.indemnizacion, registro.nuevo) == (existente, False)
    assert registro.indemnizacion.duplicados_descartados == 1
    assert (pool.prestamos, pool.conexion.transacciones) == (1, 1)  # su propia transacción


@pytest.mark.parametrize("filas_cambiadas", [1, 0])
async def test_registrar_resultado_con_la_guarda_de_pendiente(d: Any, filas_cambiadas: int) -> None:
    confirmada = d.nueva().confirmar("TRX-1", d.DESPUES)
    repo, pool = _repo([], rowcount=filas_cambiadas)
    assert await repo.registrar_resultado(confirmada) == filas_cambiadas
    [(actualizar, parametros)] = pool.conexion.cursor_.sentencias
    assert actualizar.startswith("UPDATE indemnizaciones SET estado = %s")
    assert actualizar.endswith("WHERE indemnizacion_id = %s AND estado = 'PENDIENTE'")
    for inmutable in ("siniestro_id", "referencia", "monto", "beneficiario_ref", "creado_en"):
        assert inmutable not in actualizar
    assert parametros == (
        "CONFIRMADA",
        "TRX-1",
        None,
        1,
        None,
        None,
        d.DESPUES,
        d.DESPUES,
        confirmada.indemnizacion_id,
    )


async def test_descartar_duplicado_suma_uno_en_cualquier_estado(d: Any) -> None:
    ident = uuid4()
    repo, pool = _repo([])
    await repo.descartar_duplicado(ident)
    [(sql, parametros)] = pool.conexion.cursor_.sentencias
    assert sql == (
        "UPDATE indemnizaciones SET duplicados_descartados = duplicados_descartados + 1 "
        "WHERE indemnizacion_id = %s"
    )
    assert parametros == (ident,)
    assert "estado" not in sql.split("WHERE")[1]


async def test_reclamar_usa_skip_locked_en_una_transaccion_corta(d: Any) -> None:
    a = d.nueva(ahora=d.AHORA - timedelta(minutes=30))
    b = d.nueva(ahora=d.AHORA - timedelta(minutes=60))
    hasta = d.AHORA + timedelta(seconds=60)
    filas = [_de_la_base(fila_indemnizacion(i.reclamar(hasta))) for i in (a, b)]
    repo, pool = _repo(filas)
    antes_de = d.AHORA - timedelta(minutes=5)
    reclamadas = await repo.reclamar(antes_de, d.AHORA, hasta, 10)
    # Las más antiguas primero, y con el reclamo marcado.
    assert [r.indemnizacion_id for r in reclamadas] == [b.indemnizacion_id, a.indemnizacion_id]
    assert all(r.reclamado_hasta == hasta for r in reclamadas)
    [(sql, parametros)] = pool.conexion.cursor_.sentencias
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "estado = 'PENDIENTE' AND actualizado_en < %s" in sql
    assert "reclamado_hasta IS NULL OR reclamado_hasta < %s" in sql
    assert "ORDER BY actualizado_en, indemnizacion_id LIMIT %s" in sql
    assert parametros == (antes_de, d.AHORA, 10, hasta)
    # Una sola transacción corta: la conexión vuelve al pool antes de llamar a la pasarela.
    assert (pool.prestamos, pool.conexion.transacciones) == (1, 1)


async def test_por_siniestro(d: Any) -> None:
    i = d.nueva()
    repo, pool = _repo([_de_la_base(fila_indemnizacion(i))])
    assert await repo.por_siniestro(i.siniestro_id) == [i]
    assert await repo.por_siniestro(i.siniestro_id) == []
    assert pool.conexion.sql[0].endswith("WHERE siniestro_id = %s ORDER BY creado_en")


async def test_transaccion_propia_expone_su_conexion(d: Any) -> None:
    repo, pool = _repo([], rowcount=1)
    async with repo.transaccion() as tx:
        assert tx.conexion is pool.conexion
        assert await repo.registrar_resultado(d.nueva().confirmar("T", d.DESPUES), tx) == 1
    assert (pool.prestamos, pool.conexion.transacciones) == (1, 1)


def test_el_estado_se_reconstruye(d: Any) -> None:
    fila = _de_la_base(fila_indemnizacion(d.nueva()))
    assert indemnizacion_de(fila).estado is EstadoIndemnizacion.PENDIENTE
