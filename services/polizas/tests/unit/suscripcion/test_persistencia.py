"""DecisionesPg, CodigosPg, outbox y migrar con conexiones falsas (sin base). La base real la
prueban las pruebas de módulo."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from polizas.adapters.outbound import decisiones_pg
from polizas.adapters.outbound.codigos import CodigosPg, codigo
from polizas.adapters.outbound.decisiones_pg import (
    COLUMNAS,
    DecisionesPg,
    caso_de,
    decision_de,
    fila_decision,
    filtros_sql,
    migrar,
)
from polizas.adapters.outbound.outbox_pg import evento_de
from polizas.application.suscripcion.solicitar_suscripcion import (
    ComandoSuscripcion,
    ConfigSuscripcion,
    SolicitarSuscripcion,
)
from polizas.domain.suscripcion.modelos import Actor, Decision, FiltrosBandeja, RolActor

pytestmark = pytest.mark.anyio


async def _decision(dobles: Any, sinteticos: Any, id_caso: str) -> Decision:
    caso_uso = SolicitarSuscripcion(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        ConfigSuscripcion(),
    )
    comando = ComandoSuscripcion(
        UUID(sinteticos.cotizacion_id(id_caso)),
        uuid4(),
        Actor("sub-1", RolActor.ASESOR),
        "corr",
    )
    decision, _ = await caso_uso.ejecutar(comando)
    return decision


def _como_jsonb(fila: dict[str, Any]) -> dict[str, Any]:
    """La fila tal como vuelve de PostgreSQL: los Jsonb se leen como su contenido."""
    return {k: getattr(v, "obj", v) for k, v in fila.items()}


async def test_la_fila_vuelve_igual(id_caso_decidido: str, dobles: Any, sinteticos: Any) -> None:
    decision = await _decision(dobles, sinteticos, id_caso_decidido)
    fila = fila_decision(decision)
    assert list(fila) == list(COLUMNAS)
    assert decision_de(_como_jsonb(fila)) == decision


async def test_el_evento_no_lleva_montos_ni_insumos(dobles: Any, sinteticos: Any) -> None:
    decision = await _decision(dobles, sinteticos, "S05")
    sobre = evento_de(decision)
    payload = sobre.payload.model_dump(mode="json", by_alias=True)
    assert (sobre.type, sobre.version, sobre.aggregate_id) == (
        "DecisionSuscripcionRegistrada",
        1,
        decision.id,
    )
    assert payload["reglasNoCumplidas"] == ["SUS-004", "SUS-011"]
    assert payload["hashInsumos"] == decision.hash_insumos
    texto = str(payload)
    assert "620000000" not in texto and "0.61" not in texto and "edad" not in texto


def test_filtros_de_la_bandeja() -> None:
    assert filtros_sql(FiltrosBandeja()) == (["resultado = 'REVISION_REQUERIDA'"], [])
    condiciones, parametros = filtros_sql(
        FiltrosBandeja("vida-hipotecario", "NO_ELEGIBLE", " cot-0000_1%\\ ")
    )
    assert condiciones[1:] == [
        "producto_id = %s",
        "motivos ? %s",
        "(codigo ILIKE %s OR cotizacion_id::text ILIKE %s)",
    ]
    # Los comodines de LIKE del texto buscado se escapan; COT- se quita para la cotización.
    assert parametros == [
        "vida-hipotecario",
        "NO_ELEGIBLE",
        "cot-0000\\_1\\%\\\\%",
        "0000\\_1\\%\\\\%",
    ]


def test_caso_de_una_fila() -> None:
    instante = datetime(2026, 10, 22, tzinfo=UTC)
    fila = {
        "id": uuid4(),
        "codigo": "DEC-2026-000001",
        "cotizacion_id": uuid4(),
        "cotizacion_codigo": None,
        "cliente_id": uuid4(),
        "producto_id": "vida-hipotecario",
        "moneda": "COP",
        "suma_asegurada": "620000000",
        "motivos": ["SUMA_REQUIERE_REVISION"],
        "reglas_evaluadas": [
            {"codigo": "SUS-001", "resultado": "CUMPLE"},
            {"codigo": "SUS-004", "resultado": "NO_CUMPLE"},
            {"codigo": "SUS-007", "resultado": "NO_EVALUABLE"},
        ],
        "decidida_en": instante,
    }
    caso = caso_de(fila)
    assert caso.suma_asegurada == Decimal(620000000)
    assert caso.reglas_no_cumplidas == ("SUS-004", "SUS-007")
    assert caso.antiguedad_minutos == 0


# --- Conexiones falsas ------------------------------------------------------------------------


class Cursor:
    def __init__(self, filas: list[Any]) -> None:
        self.filas = filas
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any = None) -> Self:
        self.sentencias.append((sql, parametros))
        return self

    async def fetchone(self) -> Any:
        return self.filas[0] if self.filas else None

    async def fetchall(self) -> list[Any]:
        return self.filas


class Conexion:
    def __init__(self, filas: list[Any]) -> None:
        self.cursor_ = Cursor(filas)
        self.transacciones = 0

    def cursor(self, row_factory: Any = None) -> Cursor:
        return self.cursor_

    async def execute(self, sql: str, parametros: Any = None) -> Cursor:
        return await self.cursor_.execute(sql, parametros)

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


def _repo(filas: list[Any]) -> tuple[DecisionesPg, Conexion]:
    conexion = Conexion(filas)
    return DecisionesPg(Pool(conexion)), conexion  # type: ignore[arg-type]


async def test_guardar_inserta_la_decision_y_su_evento_en_una_transaccion(
    dobles: Any, sinteticos: Any
) -> None:
    decision = await _decision(dobles, sinteticos, "S01")
    repo, conexion = _repo([(decision.id,)])
    assert await repo.guardar(decision) is True
    (insertar, _), (outbox, parametros) = conexion.cursor_.sentencias
    assert "INSERT INTO decisiones_suscripcion" in insertar
    assert "ON CONFLICT DO NOTHING RETURNING id" in insertar
    assert "INSERT INTO outbox" in outbox
    assert parametros[1:4] == (decision.id, "DecisionSuscripcionRegistrada", 1)
    assert conexion.transacciones == 1


async def test_guardar_no_escribe_el_evento_si_otra_gano(dobles: Any, sinteticos: Any) -> None:
    decision = await _decision(dobles, sinteticos, "S01")
    repo, conexion = _repo([])
    assert await repo.guardar(decision) is False
    assert len(conexion.cursor_.sentencias) == 1


async def test_lecturas(dobles: Any, sinteticos: Any) -> None:
    decision = await _decision(dobles, sinteticos, "S06")
    fila = _como_jsonb(fila_decision(decision))
    repo, conexion = _repo([fila])
    assert await repo.por_id(decision.id) == decision
    assert await repo.por_cotizacion(decision.cotizacion_id) == decision
    assert await repo.por_idempotencia(decision.idempotency_key) == decision
    sentencias = [sql for sql, _ in conexion.cursor_.sentencias]
    assert [s.split("WHERE ")[1] for s in sentencias] == [
        "id = %s",
        "cotizacion_id = %s",
        "idempotency_key = %s",
    ]
    vacio, _ = _repo([])
    assert await vacio.por_id(uuid4()) is None


async def test_bandeja_y_conteos(dobles: Any, sinteticos: Any) -> None:
    instante = datetime(2026, 10, 22, tzinfo=UTC)
    fila_caso = {
        "id": uuid4(),
        "codigo": "DEC-2026-000001",
        "cotizacion_id": uuid4(),
        "cotizacion_codigo": None,
        "cliente_id": uuid4(),
        "producto_id": "vida-hipotecario",
        "moneda": "COP",
        "suma_asegurada": "620000000",
        "motivos": [],
        "reglas_evaluadas": [],
        "decidida_en": instante,
    }
    repo, conexion = _repo([fila_caso])
    despues = (instante, uuid4())
    [caso] = await repo.bandeja(FiltrosBandeja(producto_id="vida-hipotecario"), 3, despues)
    assert caso.codigo == "DEC-2026-000001"
    sql, parametros = conexion.cursor_.sentencias[-1]
    assert "(decidida_en, id) > (%s, %s)" in sql and "ORDER BY decidida_en, id LIMIT %s" in sql
    assert parametros == ["vida-hipotecario", *despues, 3]
    conteo, _ = _repo([{"total": 7}])
    assert await conteo.contar_bandeja(FiltrosBandeja()) == 7
    dia, _ = _repo([(3, 2, Decimal("412.5"))])
    assert await dia.contar_dia(instante, instante) == (3, 2, 412.5)
    sin_datos, _ = _repo([(0, 0, None)])
    assert await sin_datos.contar_dia(instante, instante) == (0, 0, None)
    fuera, _ = _repo([(1,)])
    assert await fuera.contar_fuera_del_compromiso(instante) == 1


async def test_codigos_desde_la_secuencia() -> None:
    conexion = Conexion([(42,)])
    assert await CodigosPg(Pool(conexion)).siguiente(2026) == "DEC-2026-000042"  # type: ignore[arg-type]
    assert conexion.cursor_.sentencias == [("SELECT nextval('decisiones_codigo')", None)]
    assert codigo(2027, 1) == "DEC-2027-000001"


class ConexionSincrona:
    def __init__(self) -> None:
        self.sentencias: list[str] = []
        self.argumentos: dict[str, Any] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, parametros: Any = None) -> None:
        self.sentencias.append(sql)


def test_migrar_bloquea_aplica_y_libera(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = ConexionSincrona()

    def conectar(url: str, **argumentos: Any) -> ConexionSincrona:
        conexion.argumentos = {"url": url, **argumentos}
        return conexion

    monkeypatch.setattr(decisiones_pg.psycopg, "connect", conectar)
    migrar("postgresql://u@h/polizas")
    assert conexion.argumentos == {"url": "postgresql://u@h/polizas", "autocommit": True}
    assert "pg_advisory_lock(hashtext('polizas.migraciones'))" in conexion.sentencias[0]
    assert "CREATE TABLE IF NOT EXISTS decisiones_suscripcion" in conexion.sentencias[1]
    # HU-W15: 002 después de 001 (aceptaciones referencia a las decisiones).
    assert "CREATE TABLE IF NOT EXISTS aceptaciones" in conexion.sentencias[2]
    # HU-W16 (16.13): 003, el inbox y la proyección del recaudo.
    assert "CREATE TABLE IF NOT EXISTS inbox" in conexion.sentencias[3]
    assert "CREATE TABLE IF NOT EXISTS recaudos_aceptacion" in conexion.sentencias[3]
    assert "pg_advisory_unlock" in conexion.sentencias[-1]
    # Solo los .sql de la raíz: las reversas (down/) no se aplican al arrancar.
    assert len(conexion.sentencias) == 5


def test_migrar_libera_el_candado_si_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fallida(ConexionSincrona):
        def execute(self, sql: str, parametros: Any = None) -> None:
            super().execute(sql, parametros)
            if "CREATE TABLE" in sql:
                raise RuntimeError("falla")

    conexion = Fallida()
    monkeypatch.setattr(decisiones_pg.psycopg, "connect", lambda *_a, **_k: conexion)
    with pytest.raises(RuntimeError):
        migrar("postgresql://u@h/polizas")
    assert "pg_advisory_unlock" in conexion.sentencias[-1]
