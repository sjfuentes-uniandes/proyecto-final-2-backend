"""EntregasPg con conexión falsa (patrón de `services/consulta/tests/unit/test_adaptadores_pg.py`).

No depende de Postgres ni de las pruebas de módulo: cada método abre su propia conexión falsa o
reutiliza la de una transacción falsa, y las aserciones verifican la sentencia ejecutada y los
parámetros, sin interpretar SQL real.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from avisos.adapters.outbound.entregas_pg import EntregasPg, _codigo
from avisos.domain.avisos.modelos import FALLO_LEASE_VENCIDO, Aviso

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _aviso() -> Aviso:
    return Aviso(tipo="POLIZA_EMITIDA", poliza_id=uuid4(), event_id=uuid4())


class _Cursor:
    """Resultado de un `execute`: filas para `fetchall`/`fetchone` o un `rowcount`."""

    def __init__(self, filas: Any = None, rowcount: int = 0) -> None:
        self._filas = filas
        self.rowcount = rowcount

    async def fetchall(self) -> Any:
        return self._filas if self._filas is not None else []

    async def fetchone(self) -> Any:
        return self._filas


class _CursorConRowFactory:
    """Lo que devuelve `conexion.cursor(row_factory=...)`: el `execute` se awaitea aparte."""

    def __init__(self, fila: Any) -> None:
        self._fila = fila

    async def execute(self, *_: Any, **__: Any) -> "_CursorConRowFactory":
        return self

    async def fetchone(self) -> Any:
        return self._fila


class _SinOp:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _Conexion:
    def __init__(self, fila_cursor: Any = None, resultado: _Cursor | None = None) -> None:
        self._fila_cursor = fila_cursor
        self._resultado = resultado if resultado is not None else _Cursor()
        self.ejecutadas: list[tuple[Any, Any]] = []

    async def execute(self, sql: Any, params: Any = None) -> _Cursor:
        self.ejecutadas.append((sql, params))
        return self._resultado

    def cursor(self, row_factory: Any = None) -> _CursorConRowFactory:
        return _CursorConRowFactory(self._fila_cursor)

    def transaction(self) -> _SinOp:
        return _SinOp()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _Pool:
    def __init__(self, conexion: _Conexion) -> None:
        self._conexion = conexion

    def connection(self) -> _Conexion:
        return self._conexion


class _Tx:
    def __init__(self, conexion: _Conexion) -> None:
        self.conexion = conexion


async def test_crear_pendientes_cuenta_las_filas_insertadas() -> None:
    conexion = _Conexion(resultado=_Cursor(filas=[("x",), ("y",)]))
    adaptador = EntregasPg(_Pool(conexion))  # type: ignore[arg-type]
    creadas = await adaptador.crear_pendientes(_aviso(), uuid4(), AHORA, _Tx(conexion))
    assert creadas == 2
    assert conexion.ejecutadas[0][1]["ahora"] == AHORA


async def test_crear_pendientes_sin_dispositivos_activos() -> None:
    conexion = _Conexion(resultado=_Cursor(filas=[]))
    adaptador = EntregasPg(_Pool(conexion))  # type: ignore[arg-type]
    assert await adaptador.crear_pendientes(_aviso(), uuid4(), AHORA, _Tx(conexion)) == 0


async def test_reclamar_arma_la_entrega_de_la_fila() -> None:
    aviso = _aviso()
    fila = {
        "id": uuid4(),
        "dispositivo_id": uuid4(),
        "event_id": aviso.event_id,
        "tipo": aviso.tipo,
        "poliza_id": aviso.poliza_id,
        "intentos": 1,
        "token": "token-sintetico-entregado",
        "activo": True,
    }
    adaptador = EntregasPg(_Pool(_Conexion(fila_cursor=fila)))  # type: ignore[arg-type]
    entrega = await adaptador.reclamar(AHORA, AHORA, max_intentos=5)
    assert entrega is not None
    assert entrega.token == "token-sintetico-entregado"
    assert entrega.aviso.event_id == aviso.event_id
    assert entrega.dispositivo_activo is True


async def test_reclamar_sin_fila_devuelve_none() -> None:
    adaptador = EntregasPg(_Pool(_Conexion(fila_cursor=None)))  # type: ignore[arg-type]
    assert await adaptador.reclamar(AHORA, AHORA, max_intentos=5) is None


async def test_marcar_enviada_ejecuta_el_update_con_el_id() -> None:
    conexion = _Conexion()
    adaptador = EntregasPg(_Pool(conexion))  # type: ignore[arg-type]
    entrega_id = uuid4()
    await adaptador.marcar_enviada(entrega_id, AHORA)
    assert conexion.ejecutadas[0][1] == (AHORA, entrega_id)


@dataclass(frozen=True, slots=True)
class _EntregaMinima:
    id: UUID
    dispositivo_id: UUID


async def test_marcar_token_invalido_invalida_la_entrega_y_el_dispositivo() -> None:
    from avisos.domain.avisos.modelos import MOTIVO_TOKEN_RECHAZADO

    conexion = _Conexion()
    adaptador = EntregasPg(_Pool(conexion))  # type: ignore[arg-type]
    entrega = _EntregaMinima(id=uuid4(), dispositivo_id=uuid4())
    await adaptador.marcar_token_invalido(entrega, AHORA, MOTIVO_TOKEN_RECHAZADO)  # type: ignore[arg-type]
    assert len(conexion.ejecutadas) == 2
    assert conexion.ejecutadas[0][1] == (MOTIVO_TOKEN_RECHAZADO, AHORA, entrega.id)
    assert conexion.ejecutadas[1][1] == (
        AHORA,
        MOTIVO_TOKEN_RECHAZADO,
        AHORA,
        entrega.dispositivo_id,
    )


async def test_reprogramar_ejecuta_el_update_con_el_codigo() -> None:
    conexion = _Conexion()
    adaptador = EntregasPg(_Pool(conexion))  # type: ignore[arg-type]
    entrega_id = uuid4()
    await adaptador.reprogramar(entrega_id, AHORA, "TIMEOUT", AHORA)
    assert conexion.ejecutadas[0][1] == (AHORA, "TIMEOUT", AHORA, entrega_id)


async def test_marcar_fallida_ejecuta_el_update_con_el_codigo() -> None:
    conexion = _Conexion()
    adaptador = EntregasPg(_Pool(conexion))  # type: ignore[arg-type]
    entrega_id = uuid4()
    await adaptador.marcar_fallida(entrega_id, "REINTENTOS_AGOTADOS", AHORA)
    assert conexion.ejecutadas[0][1] == ("REINTENTOS_AGOTADOS", AHORA, entrega_id)


async def test_agotar_vencidas_devuelve_el_rowcount() -> None:
    conexion = _Conexion(resultado=_Cursor(rowcount=3))
    adaptador = EntregasPg(_Pool(conexion))  # type: ignore[arg-type]
    agotadas = await adaptador.agotar_vencidas(AHORA, max_intentos=5)
    assert agotadas == 3
    assert conexion.ejecutadas[0][1] == (FALLO_LEASE_VENCIDO, AHORA, AHORA, 5)


def test_codigo_exige_una_constante() -> None:
    assert _codigo("TIMEOUT") == "TIMEOUT"
    with pytest.raises(ValueError):
        _codigo("texto libre con espacios")
