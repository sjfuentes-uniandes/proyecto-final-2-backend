"""MT-M11-F1: entregas de avisos de `avisos` con PostgreSQL real.

Un `PolizaEmitida` sintético entra por el consumidor real (`ConsumidorInbox`, `InboxPg`, el manejador
y los casos de uso, con `EntregasPg`); el proveedor push es el enviador en memoria. Cubre un envío, el
evento duplicado (un solo aviso), el token rechazado con otro dispositivo sano, el reintento por el
barrido sin reenviar a quien ya recibió, el reclamo atómico, el lease vencido y la migración hacia
adelante, hacia atrás y otra vez hacia adelante.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from avisos.adapters.outbound.entregas_pg import EntregasPg, migrar
from avisos.application.entregas.barrido import Barrido
from avisos.application.entregas.puertos import FalloTransitorio
from avisos.config import Settings
from avisos.domain.avisos.modelos import Aviso, hash_token
from avisos.main import armar_consumidor, armar_envio
from psycopg_pool import AsyncConnectionPool
from solventa_common.inbox import MensajeSqs

pytestmark = pytest.mark.anyio


class PushEnMemoria:
    """EnviadorPush en memoria (HU-M11): doble de prueba, igual al de `tests/unit/test_enviar_entregas.py`."""

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


MIGRACIONES = resources.files("avisos.adapters.outbound").joinpath("migraciones")
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
SETTINGS = Settings(entregas_max_intentos=3, entregas_espera_inicial_s=5, entregas_lease_s=60)


class Reloj:
    def __init__(self) -> None:
        self.t = AHORA

    def ahora(self) -> datetime:
        return self.t


class ReceptorFalso:
    def __init__(self, *cuerpos: dict[str, Any]) -> None:
        self.pendientes = [
            MensajeSqs(f"m-{i}", f"r-{i}", json.dumps(c), c["type"], 1)
            for i, c in enumerate(cuerpos)
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


def _estados(base: str) -> dict[str, tuple[str, int, str | None]]:
    filas = _filas(
        base,
        "SELECT d.token, e.estado, e.intentos, e.ultimo_fallo FROM entrega e"
        " JOIN dispositivo d ON d.id = e.dispositivo_id",
    )
    return {token: (estado, intentos, fallo) for token, estado, intentos, fallo in filas}


async def test_un_dispositivo_recibe_un_aviso_con_referencia_segura(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    sobre = sobre_poliza()
    _dispositivo(base, sobre["payload"]["clienteId"], "t1")
    _dispositivo(base, uuid4(), "t-de-otro-cliente")
    receptor = ReceptorFalso(sobre)

    assert await _consumir(base, receptor, push, reloj) == 1

    ((token, aviso),) = push.enviados
    assert token == "t1"
    assert (aviso.tipo, str(aviso.poliza_id), str(aviso.event_id)) == (
        "POLIZA_EMITIDA",
        sobre["payload"]["polizaId"],
        sobre["eventId"],
    )
    assert _estados(base) == {"t1": ("ENVIADO", 1, None)}
    assert receptor.borrados == ["m-0"]


async def test_el_mismo_evento_dos_veces_deja_un_solo_aviso(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    sobre = sobre_poliza()
    _dispositivo(base, sobre["payload"]["clienteId"], "t1")
    receptor = ReceptorFalso(sobre, sobre)

    assert await _consumir(base, receptor, push, reloj) == 2

    assert len(push.enviados) == 1
    assert _filas(base, "SELECT count(*) FROM entrega") == [(1,)]
    assert _filas(base, "SELECT count(*) FROM inbox") == [(1,)]
    assert receptor.borrados == ["m-0", "m-1"]


async def test_la_clave_unica_de_entrega_impide_un_segundo_aviso_aunque_el_inbox_no_lo_vea(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    sobre = sobre_poliza()
    _dispositivo(base, sobre["payload"]["clienteId"], "t1")
    await _consumir(base, ReceptorFalso(sobre), push, reloj)
    _filas(base, "TRUNCATE inbox")

    await _consumir(base, ReceptorFalso(sobre), push, reloj)

    assert len(push.enviados) == 1
    assert _filas(base, "SELECT count(*) FROM entrega") == [(1,)]


async def test_token_rechazado_invalida_el_dispositivo_y_el_otro_recibe_su_aviso(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    sobre = sobre_poliza()
    cliente = sobre["payload"]["clienteId"]
    malo = _dispositivo(base, cliente, "t-malo")
    _dispositivo(base, cliente, "t-bueno")
    push.rechazados.add("t-malo")
    receptor = ReceptorFalso(sobre)

    assert await _consumir(base, receptor, push, reloj) == 1

    assert _estados(base) == {
        "t-malo": ("TOKEN_INVALIDO", 1, "TOKEN_RECHAZADO"),
        "t-bueno": ("ENVIADO", 1, None),
    }
    assert _filas(
        base,
        "SELECT estado, motivo_invalidez, invalidado_en IS NOT NULL FROM dispositivo WHERE id = %s",
        (malo,),
    ) == [("INVALIDO", "TOKEN_RECHAZADO", True)]
    assert receptor.borrados == ["m-0"]


async def test_un_dispositivo_invalido_no_recibe_avisos_de_eventos_nuevos(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    primero = sobre_poliza()
    cliente = primero["payload"]["clienteId"]
    _dispositivo(base, cliente, "t-malo")
    push.rechazados.add("t-malo")
    await _consumir(base, ReceptorFalso(primero), push, reloj)

    await _consumir(base, ReceptorFalso(sobre_poliza(clienteId=cliente)), push, reloj)

    assert push.intentos == ["t-malo"]
    assert _filas(base, "SELECT count(*) FROM entrega") == [(1,)]


async def test_un_fallo_transitorio_lo_reintenta_el_barrido_sin_reenviar_al_ya_enviado(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    sobre = sobre_poliza()
    cliente = sobre["payload"]["clienteId"]
    _dispositivo(base, cliente, "t-a")
    _dispositivo(base, cliente, "t-b")
    push.fallos["t-b"] = ["TIMEOUT"]
    receptor = ReceptorFalso(sobre)

    assert await _consumir(base, receptor, push, reloj) == 1

    assert receptor.borrados == ["m-0"]
    assert _estados(base) == {"t-a": ("ENVIADO", 1, None), "t-b": ("PENDIENTE", 1, "TIMEOUT")}
    assert await _barrer(base, push, reloj) == 0

    reloj.t += timedelta(seconds=5)
    assert await _barrer(base, push, reloj) == 1

    assert _estados(base) == {"t-a": ("ENVIADO", 1, None), "t-b": ("ENVIADO", 2, None)}
    assert sorted(t for t, _ in push.enviados) == ["t-a", "t-b"]
    assert push.intentos.count("t-a") == 1


async def test_agotados_los_intentos_queda_fallida_y_se_puede_volver_a_pendiente(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    sobre = sobre_poliza()
    _dispositivo(base, sobre["payload"]["clienteId"], "t1")
    push.fallos["t1"] = ["HTTP_503"] * 3
    await _consumir(base, ReceptorFalso(sobre), push, reloj)
    for _ in range(3):
        reloj.t += timedelta(minutes=10)
        await _barrer(base, push, reloj)

    assert _estados(base) == {"t1": ("FALLIDO", 3, "REINTENTOS_AGOTADOS")}
    assert push.enviados == []

    # Recuperación operativa: la entrega vuelve a PENDIENTE con los intentos en cero.
    _filas(
        base,
        "UPDATE entrega SET estado = 'PENDIENTE', intentos = 0, proximo_intento = %s RETURNING id",
        (reloj.t,),
    )
    await _barrer(base, push, reloj)
    assert _estados(base) == {"t1": ("ENVIADO", 1, None)}


async def test_dos_tareas_nunca_reclaman_la_misma_entrega(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    sobre = sobre_poliza()
    _dispositivo(base, sobre["payload"]["clienteId"], "t1")
    # Un primer fallo transitorio deja la entrega PENDIENTE y vencida: es lo único que hay por tomar.
    push.fallos["t1"] = ["TIMEOUT"]
    await _consumir(base, ReceptorFalso(sobre), push, reloj)
    reloj.t += timedelta(minutes=1)

    async with _pool(base) as pool:
        entregas = EntregasPg(pool)
        resultados = await asyncio.gather(
            *[entregas.reclamar(reloj.t, reloj.t + timedelta(seconds=60), 3) for _ in range(8)]
        )

    assert len([r for r in resultados if r is not None]) == 1


async def test_una_entrega_en_envio_con_lease_vencido_la_retoma_el_barrido(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    sobre = sobre_poliza()
    _dispositivo(base, sobre["payload"]["clienteId"], "t1")
    push.fallos["t1"] = ["TIMEOUT"]
    await _consumir(base, ReceptorFalso(sobre), push, reloj)
    reloj.t += timedelta(minutes=1)
    async with _pool(base) as pool:
        tomada = await EntregasPg(pool).reclamar(reloj.t, reloj.t + timedelta(seconds=60), 3)
    assert tomada is not None
    assert _estados(base) == {"t1": ("EN_ENVIO", 2, "TIMEOUT")}
    assert await _barrer(base, push, reloj) == 0

    reloj.t += timedelta(seconds=61)
    assert await _barrer(base, push, reloj) == 1

    assert _estados(base) == {"t1": ("ENVIADO", 3, None)}


async def test_un_tipo_ignorado_y_una_version_distinta_no_envian(
    base: str, sobre_poliza: Any, push: PushEnMemoria, reloj: Reloj
) -> None:
    ignorado = sobre_poliza(sobre={"type": "RecaudoConfirmado"})
    version = sobre_poliza(sobre={"version": 2})
    _dispositivo(base, ignorado["payload"]["clienteId"], "t1")
    receptor = ReceptorFalso(ignorado, version)

    assert await _consumir(base, receptor, push, reloj) == 1

    assert receptor.borrados == ["m-0"]
    assert push.intentos == []
    assert _filas(base, "SELECT event_type FROM inbox") == [("RecaudoConfirmado",)]


async def test_la_clave_unica_y_los_checks_protegen_los_datos(base: str) -> None:
    cliente = uuid4()
    _dispositivo(base, cliente, "t1")

    with pytest.raises(psycopg.errors.UniqueViolation):
        _dispositivo(base, uuid4(), "t1")
    with pytest.raises(psycopg.errors.CheckViolation):
        _filas(base, "UPDATE dispositivo SET motivo_invalidez = 'texto libre'")


def test_migracion_hacia_adelante_hacia_atras_y_otra_vez_hacia_adelante(base: str) -> None:
    abajo = MIGRACIONES.joinpath("down", "001_avisos.sql").read_text(encoding="utf-8")
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(abajo)
        assert conexion.execute("SELECT to_regclass('entrega')").fetchone() == (None,)
    migrar(base)
    migrar(base)
    assert _filas(base, "SELECT to_regclass('entrega')::text") == [("entrega",)]
