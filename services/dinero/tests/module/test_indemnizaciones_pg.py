"""Persistencia de la indemnización en PostgreSQL real (HU-W23; ADR 0015).

Migración 002 idempotente con su reversa (sin tocar `recaudos`), restricciones y disparador de
`indemnizaciones`, unicidad de `(siniestro_id, tipo)` ante entregas concurrentes (una sola fila y
un solo ganador), la guarda de PENDIENTE y el reclamo de la conciliación con `SKIP LOCKED`, que
coincide con la regla pura del dominio.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib import resources
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from dinero.adapters.outbound.indemnizaciones_pg import IndemnizacionesPg, indemnizacion_de
from dinero.adapters.outbound.recaudos_pg import migrar
from dinero.domain.indemnizacion.conciliacion import seleccionar_pendientes
from dinero.domain.indemnizacion.modelos import EstadoIndemnizacion, Indemnizacion
from psycopg import sql
from solventa_common import inbox

pytestmark = pytest.mark.anyio
MIGRACIONES = resources.files("dinero.adapters.outbound").joinpath("migraciones")
RECHAZOS = (psycopg.errors.IntegrityError, psycopg.errors.RaiseException)
AHORA = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
DESPUES = AHORA + timedelta(seconds=3)


def _nueva(**cambios: Any) -> Indemnizacion:
    datos: dict[str, Any] = {
        "siniestro_id": uuid4(),
        "referencia": f"IND-88-2026-{uuid4().int % 9000 + 1000}-01",
        "monto": Decimal("2500000.00"),
        "moneda": "COP",
        "beneficiario_ref": "ben-ok",
        "correlation_id": "corr-w23",
        "ahora": AHORA,
    }
    datos.update(cambios)
    return Indemnizacion.pendiente(**datos)


def ejecutar(url: str, sql: str, parametros: Any = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.execute(sql, parametros)
        return cursor.fetchall() if cursor.description else []


@pytest.fixture
def repo(pool: Any) -> IndemnizacionesPg:
    return IndemnizacionesPg(pool)


async def _tx_inbox(pool: Any) -> Any:
    return inbox.InboxPg(pool)


# --- Migración ------------------------------------------------------------------------------------
def test_la_migracion_es_idempotente_y_su_reversa_no_toca_recaudos(base: str) -> None:
    migrar(base)
    migrar(base)  # repetirla no rompe nada
    tablas = {
        f[0] for f in ejecutar(base, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    assert {"recaudos", "indemnizaciones", "outbox", "inbox"} <= tablas
    reversa = MIGRACIONES.joinpath("down", "002_indemnizaciones.sql").read_text(encoding="utf-8")
    ejecutar(base, reversa)
    ejecutar(base, reversa)  # repetible
    tablas = {
        f[0] for f in ejecutar(base, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    assert "indemnizaciones" not in tablas and {"recaudos", "outbox", "inbox"} <= tablas
    funciones = {f[0] for f in ejecutar(base, "SELECT proname FROM pg_proc")}
    assert "indemnizaciones_protegidas" not in funciones and "recaudos_protegidos" in funciones
    migrar(base)  # vuelve a aplicarse
    assert ejecutar(base, "SELECT count(*) FROM indemnizaciones") == [(0,)]


def test_las_migraciones_se_aplican_en_orden() -> None:
    nombres = sorted(f.name for f in MIGRACIONES.iterdir() if f.name.endswith(".sql"))
    assert nombres == ["001_recaudos.sql", "002_indemnizaciones.sql"]


# --- Restricciones -------------------------------------------------------------------------------
async def _insertar(repo: IndemnizacionesPg, i: Indemnizacion) -> Any:
    return await repo.registrar_pendiente(i)


async def test_una_indemnizacion_por_siniestro_y_referencia_unica(
    repo: IndemnizacionesPg, limpia: str
) -> None:
    i = _nueva()
    assert (await _insertar(repo, i)).nuevo is True
    # Otro movimiento del mismo siniestro (otra referencia) no crea otra fila.
    otra = _nueva(siniestro_id=i.siniestro_id)
    registro = await _insertar(repo, otra)
    assert (registro.nuevo, registro.indemnizacion.indemnizacion_id) == (False, i.indemnizacion_id)
    # La misma referencia de otro siniestro choca con la restricción única.
    with pytest.raises(psycopg.errors.UniqueViolation):
        await _insertar(repo, _nueva(referencia=i.referencia))
    assert ejecutar(limpia, "SELECT count(*) FROM indemnizaciones") == [(1,)]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE indemnizaciones SET tipo = 'RECAUDO'",
        "UPDATE indemnizaciones SET referencia = 'IND-1'",
        "UPDATE indemnizaciones SET monto = 0",
        "UPDATE indemnizaciones SET moneda = 'USD'",
        "UPDATE indemnizaciones SET beneficiario_ref = ' '",
        "UPDATE indemnizaciones SET motivo = 'texto libre'",
        "UPDATE indemnizaciones SET ultimo_incierto = 'texto libre'",
        "UPDATE indemnizaciones SET intentos = -1",
        "UPDATE indemnizaciones SET duplicados_descartados = -1",
        # Estados incoherentes.
        "UPDATE indemnizaciones SET estado = 'CONFIRMADA'",
        "UPDATE indemnizaciones SET estado = 'RECHAZADA', resuelto_en = now()",
        "UPDATE indemnizaciones SET transaccion_id = 'TRX'",
        "UPDATE indemnizaciones SET motivo = 'MOTIVO'",
        "UPDATE indemnizaciones SET resuelto_en = now()",
        "UPDATE indemnizaciones SET estado = 'OTRO'",
    ],
)
async def test_la_base_rechaza_lo_que_el_dominio_no_admite(
    repo: IndemnizacionesPg, limpia: str, sql: str
) -> None:
    await _insertar(repo, _nueva())
    with pytest.raises(RECHAZOS):
        ejecutar(limpia, sql)


# --- Disparador -------------------------------------------------------------------------------------
async def test_nunca_se_borra(repo: IndemnizacionesPg, limpia: str) -> None:
    await _insertar(repo, _nueva())
    with pytest.raises(psycopg.errors.RaiseException, match="no admite DELETE"):
        ejecutar(limpia, "DELETE FROM indemnizaciones")


@pytest.mark.parametrize(
    "columna",
    ["siniestro_id", "indemnizacion_id", "referencia", "beneficiario_ref", "correlation_id"],
)
async def test_lo_que_trae_el_evento_no_cambia_ni_en_una_pendiente(
    repo: IndemnizacionesPg, limpia: str, columna: str
) -> None:
    await _insertar(repo, _nueva())
    nuevo = (
        "gen_random_uuid()"
        if columna.endswith("_id") and columna != "correlation_id"
        else "'otro-valor'"
    )
    if columna == "referencia":
        nuevo = "'IND-88-2026-9999-01'"
    with pytest.raises(psycopg.errors.RaiseException, match="lo que trae el evento no cambia"):
        ejecutar(limpia, f"UPDATE indemnizaciones SET {columna} = {nuevo}")


async def test_el_monto_y_la_creacion_tampoco_cambian(repo: IndemnizacionesPg, limpia: str) -> None:
    await _insertar(repo, _nueva())
    for cambio in ("monto = monto + 1", "creado_en = creado_en + interval '1 second'"):
        with pytest.raises(psycopg.errors.RaiseException, match="no cambia"):
            ejecutar(limpia, f"UPDATE indemnizaciones SET {cambio}")


async def test_una_final_no_cambia_su_resultado_pero_si_cuenta_duplicados(
    repo: IndemnizacionesPg, limpia: str
) -> None:
    i = _nueva()
    await _insertar(repo, i)
    confirmada = i.confirmar("TRX-1", DESPUES)
    async with repo.transaccion() as tx:
        assert await repo.registrar_resultado(confirmada, tx) == 1
    for cambio in (
        "estado = 'PENDIENTE', transaccion_id = NULL, resuelto_en = NULL",
        "transaccion_id = 'OTRA'",
        "intentos = intentos + 1",
        "ultimo_incierto = 'CODIGO'",
        "reclamado_hasta = now()",
        "actualizado_en = now()",
    ):
        with pytest.raises(RECHAZOS):
            ejecutar(limpia, f"UPDATE indemnizaciones SET {cambio}")
    # Un duplicado se cuenta aunque la indemnización sea final; nunca disminuye.
    await repo.descartar_duplicado(i.indemnizacion_id)
    await repo.descartar_duplicado(i.indemnizacion_id)
    assert ejecutar(limpia, "SELECT duplicados_descartados FROM indemnizaciones") == [(2,)]
    with pytest.raises(psycopg.errors.RaiseException, match="no disminuyen"):
        ejecutar(limpia, "UPDATE indemnizaciones SET duplicados_descartados = 0")


# --- registrar_pendiente ---------------------------------------------------------------------------
async def test_el_conflicto_solo_suma_un_duplicado_y_no_toca_nada_mas(
    repo: IndemnizacionesPg, limpia: str
) -> None:
    i = _nueva()
    await _insertar(repo, i)
    antes = ejecutar(limpia, "SELECT estado, actualizado_en, intentos, monto FROM indemnizaciones")
    for _ in range(3):
        registro = await _insertar(repo, _nueva(siniestro_id=i.siniestro_id, monto=Decimal(1)))
        assert registro.nuevo is False
    despues = ejecutar(
        limpia, "SELECT estado, actualizado_en, intentos, monto FROM indemnizaciones"
    )
    assert despues == antes
    assert ejecutar(limpia, "SELECT duplicados_descartados FROM indemnizaciones") == [(3,)]


async def test_dentro_de_la_transaccion_del_inbox_y_sin_el_evento_no_queda_nada_tras_un_rollback(
    pool: Any, repo: IndemnizacionesPg, limpia: str
) -> None:
    class _Falla(Exception):
        pass

    with pytest.raises(_Falla):
        async with pool.connection() as conexion, conexion.transaction():
            tx = inbox.TransaccionInboxPg(conexion, sql.Identifier("inbox"))  # la del consumidor
            await repo.registrar_pendiente(_nueva(), tx)
            raise _Falla
    assert ejecutar(limpia, "SELECT count(*) FROM indemnizaciones") == [(0,)]


async def test_ocho_entregas_a_la_vez_dejan_una_fila_y_un_solo_ganador(
    repo: IndemnizacionesPg, limpia: str
) -> None:
    siniestro = uuid4()
    registros = await asyncio.gather(
        *(_insertar(repo, _nueva(siniestro_id=siniestro)) for _ in range(8))
    )
    assert sum(r.nuevo for r in registros) == 1
    assert ejecutar(
        limpia, "SELECT count(*), max(duplicados_descartados) FROM indemnizaciones"
    ) == [(1, 7)]


async def test_la_escala_de_la_base_se_normaliza_a_dos_decimales(
    repo: IndemnizacionesPg, limpia: str
) -> None:
    i = _nueva(monto=Decimal("2850000.5"))
    registro = await _insertar(repo, i)
    assert registro.indemnizacion.monto == Decimal("2850000.50")
    [leida] = await repo.por_siniestro(i.siniestro_id)
    assert leida.monto == Decimal("2850000.50") and leida == registro.indemnizacion


# --- registrar_resultado ----------------------------------------------------------------------------
async def test_registrar_resultado_solo_cambia_una_pendiente(
    repo: IndemnizacionesPg, limpia: str
) -> None:
    i = _nueva()
    await _insertar(repo, i)
    async with repo.transaccion() as tx:
        assert await repo.registrar_resultado(i.confirmar("TRX-1", DESPUES), tx) == 1
    async with repo.transaccion() as tx:
        # Otra tarea ya la resolvió: 0 filas y sin chocar con el disparador.
        assert await repo.registrar_resultado(i.rechazar("MOTIVO", DESPUES), tx) == 0
    [fila] = ejecutar(
        limpia, "SELECT estado, transaccion_id, motivo, intentos, resuelto_en FROM indemnizaciones"
    )
    assert fila == ("CONFIRMADA", "TRX-1", None, 1, DESPUES)


async def test_un_incierto_guarda_el_codigo_y_los_intentos(
    repo: IndemnizacionesPg, limpia: str
) -> None:
    i = _nueva()
    await _insertar(repo, i)
    async with repo.transaccion() as tx:
        await repo.registrar_resultado(
            i.registrar_incierto("ADAPTADOR_TIEMPO_AGOTADO", DESPUES), tx
        )
    [fila] = ejecutar(
        limpia, "SELECT estado, ultimo_incierto, intentos, actualizado_en FROM indemnizaciones"
    )
    assert fila == ("PENDIENTE", "ADAPTADOR_TIEMPO_AGOTADO", 1, DESPUES)


# --- reclamar (DS3-18) -----------------------------------------------------------------------------
async def _sembrar(repo: IndemnizacionesPg, url: str, **cambios: Any) -> Indemnizacion:
    i = _nueva(**cambios)
    await _insertar(repo, i)
    return i


async def test_reclamar_marca_el_reclamo_y_no_toma_dos_veces_lo_reclamado(
    repo: IndemnizacionesPg, limpia: str
) -> None:
    ahora = AHORA + timedelta(minutes=10)
    antes_de = ahora - timedelta(minutes=5)
    ids = [(await _sembrar(repo, limpia)).indemnizacion_id for _ in range(5)]
    hasta = ahora + timedelta(seconds=60)
    primera = await repo.reclamar(antes_de, ahora, hasta, 3)
    assert len(primera) == 3 and all(r.reclamado_hasta == hasta for r in primera)
    segunda = await repo.reclamar(antes_de, ahora, hasta, 10)
    assert len(segunda) == 2
    assert {r.indemnizacion_id for r in primera + segunda} == set(ids)
    assert await repo.reclamar(antes_de, ahora, hasta, 10) == []
    # Vencido el reclamo, se puede tomar de nuevo.
    despues = ahora + timedelta(seconds=61)
    assert (
        len(
            await repo.reclamar(
                despues - timedelta(minutes=5), despues, despues + timedelta(seconds=60), 10
            )
        )
        == 5
    )


async def test_dos_reclamos_a_la_vez_no_toman_la_misma_fila(
    repo: IndemnizacionesPg, limpia: str
) -> None:
    ahora = AHORA + timedelta(minutes=10)
    for _ in range(20):
        await _sembrar(repo, limpia)
    hasta = ahora + timedelta(seconds=60)
    lotes = await asyncio.gather(
        *(repo.reclamar(ahora - timedelta(minutes=5), ahora, hasta, 20) for _ in range(4))
    )
    tomadas = [r.indemnizacion_id for lote in lotes for r in lote]
    assert len(tomadas) == 20 and len(set(tomadas)) == 20


async def test_el_sql_coincide_con_la_seleccion_pura_del_dominio(
    repo: IndemnizacionesPg, limpia: str, pool: Any
) -> None:
    ahora = AHORA + timedelta(minutes=20)
    antiguedad = timedelta(minutes=5)
    sembradas = [
        await _sembrar(repo, limpia, ahora=ahora - timedelta(minutes=m))
        for m in (60, 40, 20, 8, 6, 4, 1)
    ]
    # Una resuelta y una con reclamo vigente y otra con reclamo vencido.
    resuelta = await _sembrar(repo, limpia, ahora=ahora - timedelta(minutes=50))
    async with repo.transaccion() as tx:
        await repo.registrar_resultado(resuelta.confirmar("T", ahora), tx)
    ejecutar(
        limpia,
        "UPDATE indemnizaciones SET reclamado_hasta = %s WHERE indemnizacion_id = %s",
        (ahora + timedelta(seconds=30), sembradas[1].indemnizacion_id),
    )
    ejecutar(
        limpia,
        "UPDATE indemnizaciones SET reclamado_hasta = %s WHERE indemnizacion_id = %s",
        (ahora - timedelta(seconds=30), sembradas[2].indemnizacion_id),
    )
    async with pool.connection() as conexion:
        cursor = await conexion.execute("SELECT * FROM indemnizaciones")
        nombres = [c.name for c in cursor.description or []]
        filas = [dict(zip(nombres, f, strict=True)) for f in await cursor.fetchall()]
    todas = [indemnizacion_de(f) for f in filas]
    esperadas = seleccionar_pendientes(todas, ahora, antiguedad, 4)
    reclamadas = await repo.reclamar(ahora - antiguedad, ahora, ahora + timedelta(seconds=60), 4)
    assert [r.indemnizacion_id for r in reclamadas] == [e.indemnizacion_id for e in esperadas]
    assert len(reclamadas) == 4
    assert all(r.estado is EstadoIndemnizacion.PENDIENTE for r in reclamadas)
