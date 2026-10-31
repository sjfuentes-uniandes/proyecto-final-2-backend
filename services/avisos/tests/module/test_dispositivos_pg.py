"""MT-M11-F2: dispositivos de `avisos` con PostgreSQL real (HU-M11, bloque C).

Baja y reasignación nunca dejan avisos para el cliente equivocado: ambas fallan, en la misma
transacción, las entregas PENDIENTE o EN_ENVIO del dispositivo. Dos clientes con eventos distintos
reciben cada uno solo lo suyo.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from avisos.adapters.outbound.dispositivos_pg import DispositivosPg
from avisos.adapters.outbound.entregas_pg import migrar
from avisos.application.entregas.barrido import Barrido
from avisos.application.entregas.puertos import FalloTransitorio
from avisos.config import Settings
from avisos.domain.avisos.modelos import Aviso, hash_token
from avisos.main import armar_consumidor, armar_envio
from psycopg_pool import AsyncConnectionPool
from solventa_common.inbox import MensajeSqs

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
SETTINGS = Settings(entregas_max_intentos=3, entregas_espera_inicial_s=5, entregas_lease_s=60)


class Reloj:
    def __init__(self) -> None:
        self.t = AHORA

    def ahora(self) -> datetime:
        return self.t


class PushEnMemoria:
    """Igual al de `tests/unit/test_enviar_entregas.py` y `tests/module/test_entregas_pg.py`."""

    def __init__(self) -> None:
        self.enviados: list[tuple[str, Aviso]] = []
        self.intentos: list[str] = []
        self.rechazados: set[str] = set()
        self.fallos: dict[str, list[str]] = {}

    async def enviar(self, token: str, aviso: Aviso) -> bool:
        self.intentos.append(token)
        if self.fallos.get(token):
            raise FalloTransitorio(self.fallos[token].pop(0))
        if token in self.rechazados:
            return False
        self.enviados.append((token, aviso))
        return True


class ReceptorFalso:
    def __init__(self, *cuerpos: dict[str, Any]) -> None:
        self.pendientes = [
            MensajeSqs(f"m-{i}", f"r-{i}", dumps(c), c["type"], 1) for i, c in enumerate(cuerpos)
        ]
        self.borrados: list[str] = []

    async def recibir(self) -> list[MensajeSqs]:
        lote, self.pendientes = self.pendientes, []
        return lote

    async def borrar(self, mensaje: MensajeSqs) -> None:
        self.borrados.append(mensaje.message_id)


def _filas(url: str, sql: str, parametros: Any = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.execute(sql, parametros)
        return cursor.fetchall() if cursor.description else []


def _dispositivo(url: str, cliente_id: str | UUID, token: str) -> UUID:
    with psycopg.connect(url, autocommit=True) as conexion:
        fila = conexion.execute(
            "INSERT INTO dispositivo (cliente_id, token_hash, token, registrado_en, actualizado_en)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (cliente_id, hash_token(token), token, AHORA, AHORA),
        ).fetchone()
    assert fila is not None
    return fila[0]  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def limpia(base: str) -> None:
    _filas(base, "TRUNCATE entrega, dispositivo, inbox CASCADE")


@pytest.fixture
def reloj() -> Reloj:
    return Reloj()


@pytest.fixture
def push() -> PushEnMemoria:
    return PushEnMemoria()


@asynccontextmanager
async def _pool(base: str) -> AsyncIterator[AsyncConnectionPool[Any]]:
    async with AsyncConnectionPool(base, min_size=1, max_size=4, open=False) as pool:
        yield pool


async def _consumir(base: str, receptor: ReceptorFalso, push: PushEnMemoria, reloj: Reloj) -> int:
    async with _pool(base) as pool:
        consumidor = armar_consumidor(pool, receptor, push, reloj, SETTINGS)
        return await consumidor.ejecutar_una_vez()


async def _barrer(base: str, push: PushEnMemoria, reloj: Reloj) -> int:
    async with _pool(base) as pool:
        _, enviar = armar_envio(pool, push, reloj, SETTINGS)
        return await Barrido(enviar, 10).ejecutar_una_vez()


async def _registrar(base: str, cliente_id: UUID, token: str, ahora: datetime) -> UUID:
    async with _pool(base) as pool:
        return await DispositivosPg(pool).registrar(cliente_id, token, ahora)


async def _dar_de_baja(base: str, dispositivo_id: UUID, cliente_id: UUID, ahora: datetime) -> None:
    async with _pool(base) as pool:
        await DispositivosPg(pool).dar_de_baja(dispositivo_id, cliente_id, ahora)


def _entrega(base: str, dispositivo_id: UUID) -> tuple[str, str | None, str]:
    (fila,) = _filas(
        base,
        "SELECT estado, ultimo_fallo, cliente_id FROM entrega WHERE dispositivo_id = %s",
        (dispositivo_id,),
    )
    return fila  # type: ignore[return-value]


# --- Migración 002: ejecutarla de nuevo (idempotente) no rompe nada -----------------------------
def test_migrar_es_idempotente_con_la_columna_cliente_id(base: str) -> None:
    migrar(base)  # ya se aplicó al crear el contenedor (conftest); una segunda vez no debe fallar
    columnas = _filas(
        base,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'entrega' AND column_name = 'cliente_id'",
    )
    assert columnas == [("cliente_id",)]


# --- Baja: una entrega PENDIENTE no se envía ----------------------------------------------------
async def test_dar_de_baja_con_entrega_pendiente_no_envia_nada(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    sobre = sobre_poliza()
    cliente_id = sobre["payload"]["clienteId"]
    dispositivo_id = _dispositivo(base, cliente_id, "t-baja")
    push.fallos["t-baja"] = ["TIMEOUT"]  # el primer intento falla: la entrega queda PENDIENTE
    await _consumir(base, ReceptorFalso(sobre), push, reloj)
    push.intentos.clear()

    await _dar_de_baja(base, dispositivo_id, UUID(cliente_id), AHORA)
    reloj.t += timedelta(seconds=10)  # ya venció el próximo intento: sin la baja, se reenviaría
    procesadas = await _barrer(base, push, reloj)

    assert procesadas == 0
    assert push.enviados == []
    assert push.intentos == []
    assert _entrega(base, dispositivo_id)[:2] == ("FALLIDO", "DISPOSITIVO_DADO_DE_BAJA")
    assert _filas(
        base, "SELECT estado, motivo_invalidez FROM dispositivo WHERE id = %s", (dispositivo_id,)
    ) == [("INVALIDO", "BAJA_CLIENTE")]


# --- Reasignación: una entrega PENDIENTE no se envía al nuevo dueño -----------------------------
async def test_reasignacion_con_entrega_pendiente_no_se_envia_al_nuevo_dueno(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    sobre = sobre_poliza()
    cliente_anterior = sobre["payload"]["clienteId"]
    dispositivo_id = _dispositivo(base, cliente_anterior, "t-reasignado")
    push.fallos["t-reasignado"] = ["TIMEOUT"]  # el primer intento falla: queda PENDIENTE
    await _consumir(base, ReceptorFalso(sobre), push, reloj)
    push.intentos.clear()

    cliente_nuevo = uuid4()
    reasignado_id = await _registrar(base, cliente_nuevo, "t-reasignado", AHORA)
    reloj.t += timedelta(
        seconds=10
    )  # ya venció el próximo intento: sin la reasignación, se enviaría
    procesadas = await _barrer(base, push, reloj)

    assert reasignado_id == dispositivo_id
    assert procesadas == 0
    assert push.enviados == []
    assert _entrega(base, dispositivo_id)[:2] == ("FALLIDO", "DISPOSITIVO_REASIGNADO")
    assert _filas(
        base, "SELECT cliente_id, estado FROM dispositivo WHERE id = %s", (dispositivo_id,)
    ) == [(cliente_nuevo, "ACTIVO")]


async def test_registrar_mismo_cliente_reactiva_un_dispositivo_invalido(base: str) -> None:
    cliente_id = uuid4()
    dispositivo_id = await _registrar(base, cliente_id, "t-reactivado", AHORA)
    await _dar_de_baja(base, dispositivo_id, cliente_id, AHORA)
    assert _filas(base, "SELECT estado FROM dispositivo WHERE id = %s", (dispositivo_id,)) == [
        ("INVALIDO",)
    ]

    reactivado_id = await _registrar(base, cliente_id, "t-reactivado", AHORA)

    assert reactivado_id == dispositivo_id
    assert _filas(
        base,
        "SELECT estado, invalidado_en, motivo_invalidez FROM dispositivo WHERE id = %s",
        (dispositivo_id,),
    ) == [("ACTIVO", None, None)]


# --- Dos clientes con eventos distintos: cada uno recibe solo lo suyo ---------------------------
async def test_cliente_a_y_b_con_eventos_distintos_reciben_solo_lo_suyo(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    cliente_a, cliente_b = uuid4(), uuid4()
    _dispositivo(base, cliente_a, "t-a")
    _dispositivo(base, cliente_b, "t-b")
    sobre_a = sobre_poliza(clienteId=str(cliente_a))
    sobre_b = sobre_poliza(clienteId=str(cliente_b))

    assert await _consumir(base, ReceptorFalso(sobre_a, sobre_b), push, reloj) == 2

    enviados = {token: aviso.event_id for token, aviso in push.enviados}
    assert set(enviados) == {"t-a", "t-b"}
    assert str(enviados["t-a"]) == sobre_a["eventId"]
    assert str(enviados["t-b"]) == sobre_b["eventId"]
    dueños = _filas(
        base,
        "SELECT d.token, e.cliente_id FROM entrega e JOIN dispositivo d ON d.id = e.dispositivo_id",
    )
    assert set(dueños) == {("t-a", cliente_a), ("t-b", cliente_b)}
