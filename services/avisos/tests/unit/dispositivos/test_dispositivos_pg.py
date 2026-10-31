"""DispositivosPg con conexión falsa (patrón de `services/avisos/tests/unit/test_entregas_pg.py`).

Cada caso configura la secuencia de filas que devolverían los `SELECT ... FOR UPDATE` y verifica
tanto el resultado como las sentencias ejecutadas (sin interpretar SQL real), incluida la ausencia
de una tercera sentencia cuando no hace falta fallar entregas.
"""

from datetime import UTC, datetime
from typing import Any, Self
from uuid import uuid4

import pytest
from avisos.adapters.outbound.dispositivos_pg import DispositivosPg
from avisos.application.dispositivos.puertos import DispositivoNoAutorizado, DispositivoNoEncontrado
from avisos.domain.avisos.modelos import (
    FALLO_DISPOSITIVO_DADO_DE_BAJA,
    FALLO_DISPOSITIVO_REASIGNADO,
    MOTIVO_BAJA_CLIENTE,
    hash_token,
)

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TOKEN = "token-sintetico-entregado"


class _Cursor:
    def __init__(self, fila: Any) -> None:
        self._fila = fila

    async def fetchone(self) -> Any:
        return self._fila


class _SinOp:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _Conexion:
    """Devuelve, en orden, una fila configurada por cada `execute`; registra sql y parámetros."""

    def __init__(self, *resultados: Any) -> None:
        self._resultados = list(resultados)
        self.ejecutadas: list[tuple[Any, Any]] = []

    async def execute(self, sql: Any, params: Any = None) -> _Cursor:
        self.ejecutadas.append((sql, params))
        fila = self._resultados.pop(0) if self._resultados else None
        return _Cursor(fila)

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


# --- registrar: token nuevo ------------------------------------------------------------------
async def test_registrar_token_nuevo_inserta_un_dispositivo() -> None:
    cliente_id = uuid4()
    nuevo_id = uuid4()
    conexion = _Conexion(None, (nuevo_id,))
    adaptador = DispositivosPg(_Pool(conexion))  # type: ignore[arg-type]

    resultado = await adaptador.registrar(cliente_id, TOKEN, AHORA)

    assert resultado == nuevo_id
    assert len(conexion.ejecutadas) == 2
    assert conexion.ejecutadas[0][1] == (hash_token(TOKEN),)
    assert conexion.ejecutadas[1][1] == (cliente_id, hash_token(TOKEN), TOKEN, AHORA, AHORA)


# --- registrar: mismo cliente (idempotente o reactiva un INVALIDO) ---------------------------
async def test_registrar_mismo_cliente_no_falla_entregas() -> None:
    cliente_id = uuid4()
    dispositivo_id = uuid4()
    conexion = _Conexion((dispositivo_id, cliente_id), None)
    adaptador = DispositivosPg(_Pool(conexion))  # type: ignore[arg-type]

    resultado = await adaptador.registrar(cliente_id, TOKEN, AHORA)

    assert resultado == dispositivo_id
    assert len(conexion.ejecutadas) == 2  # sin la sentencia de fallar entregas
    assert conexion.ejecutadas[1][1] == (cliente_id, TOKEN, AHORA, dispositivo_id)
    assert "ACTIVO" in conexion.ejecutadas[1][0]
    assert "invalidado_en = NULL" in conexion.ejecutadas[1][0]


# --- registrar: otro cliente (reasignación) ---------------------------------------------------
async def test_registrar_otro_cliente_reasigna_y_falla_las_entregas_pendientes() -> None:
    cliente_nuevo = uuid4()
    cliente_anterior = uuid4()
    dispositivo_id = uuid4()
    conexion = _Conexion((dispositivo_id, cliente_anterior), None, None)
    adaptador = DispositivosPg(_Pool(conexion))  # type: ignore[arg-type]

    resultado = await adaptador.registrar(cliente_nuevo, TOKEN, AHORA)

    assert resultado == dispositivo_id
    assert len(conexion.ejecutadas) == 3
    assert conexion.ejecutadas[1][1] == (cliente_nuevo, TOKEN, AHORA, dispositivo_id)
    assert conexion.ejecutadas[2][1] == (FALLO_DISPOSITIVO_REASIGNADO, AHORA, dispositivo_id)


async def test_registrar_nunca_envia_el_token_a_la_busqueda() -> None:
    conexion = _Conexion(None, (uuid4(),))
    adaptador = DispositivosPg(_Pool(conexion))  # type: ignore[arg-type]
    await adaptador.registrar(uuid4(), TOKEN, AHORA)
    assert TOKEN not in str(conexion.ejecutadas[0])


# --- dar_de_baja ------------------------------------------------------------------------------
async def test_dar_de_baja_sin_dispositivo_lanza_no_encontrado() -> None:
    conexion = _Conexion(None)
    adaptador = DispositivosPg(_Pool(conexion))  # type: ignore[arg-type]
    with pytest.raises(DispositivoNoEncontrado):
        await adaptador.dar_de_baja(uuid4(), uuid4(), AHORA)
    assert len(conexion.ejecutadas) == 1  # no se toca nada más


async def test_dar_de_baja_de_otro_cliente_lanza_no_autorizado_sin_mutar() -> None:
    dispositivo_id, dueno, otro = uuid4(), uuid4(), uuid4()
    conexion = _Conexion((dispositivo_id, dueno))
    adaptador = DispositivosPg(_Pool(conexion))  # type: ignore[arg-type]
    with pytest.raises(DispositivoNoAutorizado):
        await adaptador.dar_de_baja(dispositivo_id, otro, AHORA)
    assert len(conexion.ejecutadas) == 1


async def test_dar_de_baja_invalida_el_dispositivo_y_falla_sus_entregas() -> None:
    dispositivo_id, cliente_id = uuid4(), uuid4()
    conexion = _Conexion((dispositivo_id, cliente_id), None, None)
    adaptador = DispositivosPg(_Pool(conexion))  # type: ignore[arg-type]

    await adaptador.dar_de_baja(dispositivo_id, cliente_id, AHORA)

    assert len(conexion.ejecutadas) == 3
    assert conexion.ejecutadas[1][1] == (AHORA, MOTIVO_BAJA_CLIENTE, AHORA, dispositivo_id)
    assert conexion.ejecutadas[2][1] == (FALLO_DISPOSITIVO_DADO_DE_BAJA, AHORA, dispositivo_id)


def test_dispositivo_no_encontrado_y_no_autorizado_son_excepciones_distintas() -> None:
    assert issubclass(DispositivoNoEncontrado, Exception)
    assert issubclass(DispositivoNoAutorizado, Exception)
    assert not issubclass(DispositivoNoEncontrado, DispositivoNoAutorizado)
