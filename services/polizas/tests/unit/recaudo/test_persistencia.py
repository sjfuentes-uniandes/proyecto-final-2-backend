"""RecaudosPg con conexiones falsas (sin base), HU-W16, 16.13. La base real la prueba
`tests/module/test_recaudo_w16.py`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self
from uuid import uuid4

import pytest
from polizas.adapters.outbound.recaudos_pg import (
    COLUMNAS,
    RecaudosPg,
    fila_recaudo,
    recaudo_de,
)
from polizas.domain.recaudo.modelos import ResultadoRecaudo, SituacionRecaudo
from psycopg import sql
from solventa_common.inbox import TransaccionInboxPg

pytestmark = pytest.mark.anyio
INSTANTE = datetime(2026, 10, 24, 15, 0, 1, tzinfo=UTC)
CONFIRMADO = ResultadoRecaudo(
    clave_recaudo=uuid4(),
    recaudo_id=uuid4(),
    estado=SituacionRecaudo.CONFIRMADO,
    monto=Decimal("64638.10"),
    moneda="COP",
    transaccion_id="TX-1",
    motivo=None,
    ocurrido_en=INSTANTE,
)
RECHAZADO = ResultadoRecaudo(
    clave_recaudo=uuid4(),
    recaudo_id=uuid4(),
    estado=SituacionRecaudo.RECHAZADO,
    monto=None,
    moneda=None,
    transaccion_id=None,
    motivo="FONDOS_INSUFICIENTES",
    ocurrido_en=INSTANTE,
)


class Cursor:
    """Devuelve, por cada sentencia, la siguiente fila de `respuestas` (None si se acaban)."""

    def __init__(self, respuestas: list[Any]) -> None:
        self.respuestas = respuestas
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any = None) -> Self:
        self.sentencias.append((sql, parametros))
        return self

    async def fetchone(self) -> Any:
        return self.respuestas.pop(0) if self.respuestas else None


class Conexion:
    def __init__(self, respuestas: list[Any]) -> None:
        self.cursor_ = Cursor(respuestas)
        self.fabricas: list[Any] = []

    def cursor(self, row_factory: Any = None) -> Cursor:
        self.fabricas.append(row_factory)
        return self.cursor_

    async def execute(self, sql: str, parametros: Any = None) -> Cursor:
        return await self.cursor_.execute(sql, parametros)


class Pool:
    def __init__(self, conexion: Conexion) -> None:
        self.conexion = conexion

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Conexion]:
        yield self.conexion


def _tx(respuestas: list[Any]) -> tuple[RecaudosPg, TransaccionInboxPg, Conexion]:
    """El repositorio con un pool que no se debe usar y la transacción del inbox."""
    conexion = Conexion(respuestas)
    sin_usar = Pool(Conexion([]))
    tx = TransaccionInboxPg(conexion, sql.Identifier("inbox"))  # type: ignore[arg-type]
    return RecaudosPg(sin_usar), tx, conexion  # type: ignore[arg-type]


@pytest.mark.parametrize("resultado", [CONFIRMADO, RECHAZADO], ids=["confirmado", "rechazado"])
def test_la_fila_vuelve_igual(resultado: ResultadoRecaudo) -> None:
    fila = fila_recaudo(resultado)
    assert list(fila) == list(COLUMNAS)
    assert fila["estado"] == resultado.estado.value
    assert recaudo_de(fila) == resultado


def test_el_monto_va_como_decimal() -> None:
    assert isinstance(fila_recaudo(CONFIRMADO)["monto"], Decimal)
    assert fila_recaudo(RECHAZADO)["monto"] is None


async def test_guardar_inserta_en_la_transaccion_del_inbox() -> None:
    repo, tx, conexion = _tx([(CONFIRMADO.clave_recaudo,)])
    assert await repo.guardar(CONFIRMADO, tx) is None
    [(insertar, parametros)] = conexion.cursor_.sentencias
    assert insertar.startswith("INSERT INTO recaudos_aceptacion (")
    assert insertar.endswith("ON CONFLICT (clave_recaudo) DO NOTHING RETURNING clave_recaudo")
    assert list(parametros) == [fila_recaudo(CONFIRMADO)[c] for c in COLUMNAS]


async def test_guardar_con_un_estado_previo_no_cambia_nada_y_lo_devuelve() -> None:
    previo = fila_recaudo(CONFIRMADO)
    repo, tx, conexion = _tx([None, previo])
    assert await repo.guardar(RECHAZADO, tx) == CONFIRMADO
    (_, _), (leer, parametros) = conexion.cursor_.sentencias
    assert "FROM recaudos_aceptacion WHERE clave_recaudo = %s" in leer
    assert parametros == (RECHAZADO.clave_recaudo,)
    assert not any(s.lstrip().startswith("UPDATE") for s, _ in conexion.cursor_.sentencias)
    assert conexion.fabricas and conexion.fabricas[-1] is not None


async def test_guardar_sin_fila_tras_el_conflicto_falla() -> None:
    # No pasa (el conflicto implica una fila confirmada); si pasara, rollback y reentrega.
    repo, tx, conexion = _tx([None, None])
    with pytest.raises(RuntimeError, match="sin fila"):
        await repo.guardar(CONFIRMADO, tx)
    assert len(conexion.cursor_.sentencias) == 2


@pytest.mark.parametrize(("fila", "esperado"), [((1,), True), (None, False)])
async def test_aceptacion_conocida(fila: Any, esperado: bool) -> None:
    repo, tx, conexion = _tx([fila])
    clave = uuid4()
    assert await repo.aceptacion_conocida(clave, tx) is esperado
    [(consulta, parametros)] = conexion.cursor_.sentencias
    assert "FROM aceptaciones WHERE id = %s" in consulta
    assert parametros == (clave,)


async def test_por_clave_lee_con_el_pool() -> None:
    conexion = Conexion([fila_recaudo(RECHAZADO)])
    repo = RecaudosPg(Pool(conexion))  # type: ignore[arg-type]
    assert await repo.por_clave(RECHAZADO.clave_recaudo) == RECHAZADO
    [(consulta, parametros)] = conexion.cursor_.sentencias
    assert consulta.endswith("WHERE clave_recaudo = %s")
    assert parametros == (RECHAZADO.clave_recaudo,)
    vacio = RecaudosPg(Pool(Conexion([])))  # type: ignore[arg-type]
    assert await vacio.por_clave(uuid4()) is None
