"""Persistencia del movimiento de recaudo en PostgreSQL real (HU-W16, 16.9; plan §d).

Migración idempotente con su reversa, restricciones y disparador de `recaudos`, unicidad de
`claveRecaudo` ante entregas concurrentes, guarda de `PENDIENTE` y operaciones dentro de la
transacción del inbox (`solventa_common.inbox.InboxPg`), que es la que usará el consumidor (16.10).
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from dinero.adapters.outbound.recaudos_pg import COLUMNAS, RecaudosPg, fila_recaudo, migrar
from dinero.config import Settings
from dinero.domain.recaudo.modelos import EstadoRecaudo, Recaudo
from fastapi.testclient import TestClient
from solventa_common import inbox

pytestmark = pytest.mark.anyio
MIGRACIONES = resources.files("dinero.adapters.outbound").joinpath("migraciones")
RAIZ = Path(__file__).resolve().parents[4]
OUTBOX_POLIZAS = (
    RAIZ / "services/polizas/src/polizas/adapters/outbound/migraciones/001_suscripcion.sql"
)
AHORA = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
DESPUES = AHORA + timedelta(seconds=3)
PAGADOR = UUID("00000000-0000-4000-8000-000000000001")


def _pendiente(**cambios: Any) -> Recaudo:
    datos: dict[str, Any] = {
        "clave_recaudo": uuid4(),
        "cotizacion_id": uuid4(),
        "pagador_ref": PAGADOR,
        "monto": Decimal(64638),
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "correlation_id": "corr-w16",
        "ahora": AHORA,
    }
    datos.update(cambios)
    return Recaudo.pendiente(**datos)


def ejecutar(url: str, sql: str, parametros: Any = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.execute(sql, parametros)
        return cursor.fetchall() if cursor.description else []


@dataclass(frozen=True)
class Transaccion:
    conexion: Any


def _contar(url: str, tabla: str = "recaudos") -> int:
    [(cuantas,)] = ejecutar(url, f"SELECT count(*) FROM {tabla}")
    return int(cuantas)


def _insertar(url: str, **cambios: Any) -> None:
    fila = fila_recaudo(_pendiente())
    fila.update(cambios)
    ejecutar(
        url,
        f"INSERT INTO recaudos ({', '.join(COLUMNAS)}) VALUES ({', '.join(['%s'] * len(COLUMNAS))})",
        [fila[c] for c in COLUMNAS],
    )


# --- Migración ----------------------------------------------------------------------------------
def _existe(url: str, objeto: str) -> bool:
    [(existe,)] = ejecutar(url, "SELECT to_regclass(%s) IS NOT NULL", (objeto,))
    return bool(existe)


async def test_migracion_adelante_atras_y_adelante(base: str) -> None:
    """Aplicar (ya aplicada por el arnés, y otra vez: idempotente), revertir dos veces y volver a
    aplicar."""
    migrar(base)
    reversa = MIGRACIONES.joinpath("down", "001_recaudos.sql").read_text(encoding="utf-8")
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(reversa)
        conexion.execute(reversa)
    for tabla in ("recaudos", "outbox", "inbox"):
        assert not _existe(base, tabla), tabla
    funcion = "SELECT count(*) FROM pg_proc WHERE proname = 'recaudos_protegidos'"
    assert ejecutar(base, funcion) == [(0,)]
    migrar(base)
    migrar(base)
    for tabla in ("recaudos", "outbox", "inbox"):
        assert _existe(base, tabla), tabla
    disparador = "SELECT count(*) FROM pg_trigger WHERE tgname = 'recaudos_protegidos'"
    assert ejecutar(base, disparador) == [(1,)]
    assert _contar(base) == 0


def _forma(url: str, esquema: str, tabla: str) -> tuple[Any, ...]:
    columnas = ejecutar(
        url,
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position",
        (esquema, tabla),
    )
    indices = ejecutar(
        url,
        "SELECT indexname, replace(indexdef, %s, '') FROM pg_indexes "
        "WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
        (f"{esquema}.", esquema, tabla),
    )
    return columnas, indices


def _forma_en_esquema(url: str, ddl: str, tabla: str) -> tuple[Any, ...]:
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute("DROP SCHEMA IF EXISTS modelo CASCADE")
        conexion.execute("CREATE SCHEMA modelo")
        conexion.execute("SET search_path TO modelo")
        conexion.execute(ddl)
    try:
        return _forma(url, "modelo", tabla)
    finally:
        ejecutar(url, "DROP SCHEMA modelo CASCADE")


async def test_el_outbox_tiene_la_forma_del_de_polizas(limpia: str) -> None:
    """ADR 0013 y CLAUDE.md: `published_at` nullable, `attempts`, `last_error` e índice parcial."""
    modelo = _forma_en_esquema(limpia, OUTBOX_POLIZAS.read_text(encoding="utf-8"), "outbox")
    assert _forma(limpia, "public", "outbox") == modelo
    columnas, indices = modelo
    assert ("published_at", "timestamp with time zone", "YES", None) in columnas
    assert any("WHERE (published_at IS NULL)" in definicion for _, definicion in indices)


async def test_el_inbox_tiene_el_ddl_de_la_libreria(limpia: str) -> None:
    ddl = re.search(r"```sql\n(.*?)```", inbox.__doc__ or "", re.DOTALL)
    assert ddl is not None
    assert _forma(limpia, "public", "inbox") == _forma_en_esquema(limpia, ddl.group(1), "inbox")


# --- Restricciones -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cambios",
    [
        {"monto": Decimal(0)},
        {"monto": Decimal("-0.01")},
        {"moneda": "USD"},
        # Con resuelto_en, solo el CHECK de estado lo rechaza (sin él, recaudos_resuelto_si_final).
        {"estado": "ANULADO", "resuelto_en": DESPUES},
        {"estado": "ANULADO"},
        {"estado": "CONFIRMADO", "resuelto_en": DESPUES},  # sin transaccion_id
        {"estado": "RECHAZADO", "resuelto_en": DESPUES},  # sin motivo
        {"transaccion_id": "TX"},  # un PENDIENTE no tiene resultado definitivo
        {"motivo": "FONDOS_INSUFICIENTES"},
        {"resuelto_en": DESPUES},
        {"estado": "CONFIRMADO", "transaccion_id": "TX"},  # final sin resuelto_en
        {"estado": "CONFIRMADO", "transaccion_id": "", "resuelto_en": DESPUES},
        {"estado": "RECHAZADO", "motivo": "Juan Pérez", "resuelto_en": DESPUES},
        {"ultimo_incierto": "timeout tras 5000 ms"},
        {"periodicidad": ""},
    ],
    ids=lambda c: "-".join(f"{k}={v}" for k, v in c.items()),
)
async def test_cada_check_rechaza_la_fila(limpia: str, cambios: dict[str, Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insertar(limpia, **cambios)
    assert _contar(limpia) == 0


async def test_filas_validas_en_cada_estado(limpia: str) -> None:
    _insertar(limpia)
    _insertar(limpia, ultimo_incierto="TIEMPO_AGOTADO")
    _insertar(limpia, estado="CONFIRMADO", transaccion_id="TX-SINT-C01", resuelto_en=DESPUES)
    _insertar(limpia, estado="RECHAZADO", motivo="FONDOS_INSUFICIENTES", resuelto_en=DESPUES)
    _insertar(
        limpia,
        estado="RECHAZADO",
        motivo="FONDOS_INSUFICIENTES",
        transaccion_id="TX-R",
        resuelto_en=DESPUES,
    )
    assert _contar(limpia) == 5


async def test_clave_recaudo_unica(limpia: str) -> None:
    clave = uuid4()
    _insertar(limpia, clave_recaudo=clave)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insertar(limpia, clave_recaudo=clave)


# --- Disparador ----------------------------------------------------------------------------------
async def _guardado(recaudos: RecaudosPg, recaudo: Recaudo) -> Recaudo:
    assert (await recaudos.crear_pendiente(recaudo)).nuevo
    return recaudo


@pytest.mark.parametrize("estado_final", ["CONFIRMADO", "RECHAZADO"])
async def test_un_estado_final_no_se_actualiza(
    recaudos: RecaudosPg, limpia: str, estado_final: str
) -> None:
    pendiente = await _guardado(recaudos, _pendiente())
    final = (
        pendiente.confirmar("TX-SINT-C01", DESPUES)
        if estado_final == "CONFIRMADO"
        else pendiente.rechazar("FONDOS_INSUFICIENTES", DESPUES)
    )
    assert await recaudos.registrar_resultado(final) == 1
    for sentencia in (
        (
            "UPDATE recaudos SET estado = 'PENDIENTE', transaccion_id = NULL, motivo = NULL, "
            "resuelto_en = NULL WHERE recaudo_id = %s"
        ),
        "UPDATE recaudos SET ultimo_incierto = 'TIEMPO_AGOTADO' WHERE recaudo_id = %s",
        "UPDATE recaudos SET actualizado_en = actualizado_en WHERE recaudo_id = %s",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="recaudos"):
            ejecutar(limpia, sentencia, (final.recaudo_id,))
    assert await recaudos.por_clave(final.clave_recaudo) == final


@pytest.mark.parametrize("confirmado", [False, True], ids=["PENDIENTE", "CONFIRMADO"])
async def test_ningun_movimiento_se_borra(
    recaudos: RecaudosPg, limpia: str, confirmado: bool
) -> None:
    recaudo = await _guardado(recaudos, _pendiente())
    if confirmado:
        await recaudos.registrar_resultado(recaudo.confirmar("TX-SINT-C01", DESPUES))
    with pytest.raises(psycopg.errors.RaiseException, match="recaudos"):
        ejecutar(limpia, "DELETE FROM recaudos WHERE recaudo_id = %s", (recaudo.recaudo_id,))
    assert _contar(limpia) == 1


@pytest.mark.parametrize(
    "asignacion",
    [
        "monto = 1",
        "moneda = moneda",  # aun sin cambiar el valor de los demás, no se toca lo del evento
        "clave_recaudo = gen_random_uuid()",
        "pagador_ref = gen_random_uuid()",
        "cotizacion_id = gen_random_uuid()",
        "recaudo_id = gen_random_uuid()",
        "periodicidad = 'ANUAL'",
        "creado_en = now()",
        "correlation_id = 'otra'",
    ],
)
async def test_lo_que_trae_el_evento_no_cambia_ni_en_pendiente(
    recaudos: RecaudosPg, limpia: str, asignacion: str
) -> None:
    recaudo = await _guardado(recaudos, _pendiente())
    sentencia = f"UPDATE recaudos SET {asignacion} WHERE recaudo_id = %s"
    if asignacion == "moneda = moneda":
        ejecutar(limpia, sentencia, (recaudo.recaudo_id,))  # mismo valor: se admite
    else:
        with pytest.raises(psycopg.errors.RaiseException, match="recaudos"):
            ejecutar(limpia, sentencia, (recaudo.recaudo_id,))
    assert await recaudos.por_clave(recaudo.clave_recaudo) == recaudo


# --- Repositorio ---------------------------------------------------------------------------------
async def test_crear_pendiente_repetido_devuelve_el_existente(
    recaudos: RecaudosPg, limpia: str
) -> None:
    primero = _pendiente()
    repetido = _pendiente(clave_recaudo=primero.clave_recaudo, monto=Decimal(1))
    assert (await recaudos.crear_pendiente(primero)).nuevo is True
    segunda = await recaudos.crear_pendiente(repetido)
    assert (segunda.nuevo, segunda.recaudo) == (False, primero)
    assert _contar(limpia) == 1


async def test_dos_entregas_concurrentes_dejan_una_sola_fila(
    pool: Any, recaudos: RecaudosPg, limpia: str
) -> None:
    """Plan §d, concurrencia: la segunda espera al commit de la primera (índice único) y la ve
    como existente; no se marca como nueva, así que no cobraría."""
    primera = _pendiente()
    segunda = _pendiente(clave_recaudo=primera.clave_recaudo)
    async with pool.connection() as conexion, conexion.transaction():
        creada = await recaudos.crear_pendiente(primera, Transaccion(conexion))
        en_espera = asyncio.create_task(recaudos.crear_pendiente(segunda))
        await asyncio.sleep(0.5)
        assert not en_espera.done()  # bloqueada por la fila sin confirmar
    vista = await en_espera
    assert (creada.nuevo, vista.nuevo) == (True, False)
    assert vista.recaudo == primera
    assert _contar(limpia) == 1


async def test_veinte_entregas_concurrentes_una_sola_nueva(
    recaudos: RecaudosPg, limpia: str
) -> None:
    clave = uuid4()
    creaciones = await asyncio.gather(
        *(recaudos.crear_pendiente(_pendiente(clave_recaudo=clave)) for _ in range(20))
    )
    nuevas = [c for c in creaciones if c.nuevo]
    assert len(nuevas) == 1
    assert {c.recaudo for c in creaciones} == {nuevas[0].recaudo}
    assert _contar(limpia) == 1


async def test_por_clave_inexistente(recaudos: RecaudosPg, limpia: str) -> None:
    assert await recaudos.por_clave(uuid4()) is None


async def test_registrar_resultado_con_la_guarda(recaudos: RecaudosPg, limpia: str) -> None:
    pendiente = await _guardado(recaudos, _pendiente())
    incierto = pendiente.registrar_incierto("TIEMPO_AGOTADO", DESPUES)
    assert await recaudos.registrar_resultado(incierto) == 1
    assert await recaudos.por_clave(pendiente.clave_recaudo) == incierto
    confirmado = incierto.confirmar("TX-SINT-C06", DESPUES + timedelta(minutes=5))
    assert await recaudos.registrar_resultado(confirmado) == 1
    # Un segundo intento (otra tarea con la copia vieja) no cambia nada ni choca con el disparador.
    assert (
        await recaudos.registrar_resultado(incierto.rechazar("FONDOS_INSUFICIENTES", DESPUES)) == 0
    )
    assert await recaudos.registrar_resultado(confirmado) == 0
    assert await recaudos.por_clave(pendiente.clave_recaudo) == confirmado


async def test_dos_resultados_concurrentes_solo_uno_cambia_la_fila(
    recaudos: RecaudosPg, limpia: str
) -> None:
    pendiente = await _guardado(recaudos, _pendiente())
    cambios = await asyncio.gather(
        recaudos.registrar_resultado(pendiente.confirmar("TX-SINT-C01", DESPUES)),
        recaudos.registrar_resultado(pendiente.rechazar("FONDOS_INSUFICIENTES", DESPUES)),
    )
    assert sorted(cambios) == [0, 1]
    leido = await recaudos.por_clave(pendiente.clave_recaudo)
    assert leido is not None and leido.final


@pytest.mark.parametrize("texto", ["64638", "0.01", "999999999.99"])
async def test_ida_y_vuelta_exacta_del_monto(recaudos: RecaudosPg, limpia: str, texto: str) -> None:
    recaudo = await _guardado(recaudos, _pendiente(monto=Decimal(texto)))
    leido = await recaudos.por_clave(recaudo.clave_recaudo)
    assert leido is not None
    assert isinstance(leido.monto, Decimal)
    assert str(leido.monto) == texto
    assert ejecutar(limpia, "SELECT monto::text FROM recaudos") == [(texto,)]


# --- Dentro de la transacción del inbox ------------------------------------------------------------
class Abortar(Exception):
    pass


async def test_en_la_transaccion_del_inbox_que_hace_rollback_no_queda_nada(
    pool: Any, recaudos: RecaudosPg, limpia: str
) -> None:
    recaudo = _pendiente()
    with pytest.raises(Abortar):
        async with inbox.InboxPg(pool).transaccion() as tx:
            assert await tx.registrar(uuid4(), "OfertaAceptada", AHORA)
            assert (await recaudos.crear_pendiente(recaudo, tx)).nuevo
            assert await recaudos.por_clave(recaudo.clave_recaudo, tx) == recaudo
            confirmado = recaudo.confirmar("TX-SINT-C01", DESPUES)
            assert await recaudos.registrar_resultado(confirmado, tx) == 1
            raise Abortar
    assert (_contar(limpia), _contar(limpia, "inbox")) == (0, 0)
    assert await recaudos.por_clave(recaudo.clave_recaudo) is None


async def test_en_la_transaccion_del_inbox_que_confirma_queda_todo(
    pool: Any, recaudos: RecaudosPg, limpia: str
) -> None:
    recaudo = _pendiente()
    async with inbox.InboxPg(pool).transaccion() as tx:
        assert await tx.registrar(uuid4(), "OfertaAceptada", AHORA)
        assert (await recaudos.crear_pendiente(recaudo, tx)).nuevo
    assert (_contar(limpia), _contar(limpia, "inbox")) == (1, 1)
    leido = await recaudos.por_clave(recaudo.clave_recaudo)
    assert leido is not None and leido.estado is EstadoRecaudo.PENDIENTE


# --- Ciclo de vida -------------------------------------------------------------------------------
def test_el_arranque_migra_y_abre_el_pool(base: str) -> None:
    """Con la base revertida, arrancar la app vuelve a crear las tablas; al cerrar, el pool se
    cierra."""
    reversa = MIGRACIONES.joinpath("down", "001_recaudos.sql").read_text(encoding="utf-8")
    ejecutar(base, reversa)
    assert not _existe(base, "recaudos")
    app = crear_app_con_base(base)
    with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as cliente:
        assert _existe(base, "recaudos") and _existe(base, "outbox") and _existe(base, "inbox")
        pool = cliente.app.state.pool  # type: ignore[attr-defined]
        assert cliente.get("/health").json() == {"status": "ok"}
        assert not pool.closed
    assert pool.closed


def crear_app_con_base(url: str) -> Any:
    from dinero.main import crear_app

    return crear_app(Settings(SOLVENTA_DATABASE_URL=url))
