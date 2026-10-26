"""AceptacionesPg con conexiones falsas (sin base). La base real la prueba MT-W15-01."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from polizas.adapters.outbound.aceptaciones_pg import (
    COLUMNAS,
    AceptacionesPg,
    aceptacion_de,
    fila_aceptacion,
)
from polizas.application.aceptacion.registrar import (
    ComandoAceptacion,
    ConfigAceptacion,
    RegistrarAceptacion,
)
from polizas.application.suscripcion.solicitar_suscripcion import (
    ComandoSuscripcion,
    ConfigSuscripcion,
    SolicitarSuscripcion,
)
from polizas.domain.aceptacion.evidencia import huella_evidencia
from polizas.domain.aceptacion.modelos import Aceptacion, CanalAceptacion, Declaraciones
from polizas.domain.suscripcion.modelos import Actor, Decision, RolActor

pytestmark = pytest.mark.anyio
ASESOR = Actor("sub-asesor-1", RolActor.ASESOR)


async def _aceptada(dobles: Any, sinteticos: Any) -> tuple[Aceptacion, Decision]:
    decision, _ = await SolicitarSuscripcion(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        ConfigSuscripcion(),
    ).ejecutar(ComandoSuscripcion(UUID(sinteticos.cotizacion_id("S01")), uuid4(), ASESOR, "c"))
    aceptacion, _ = await RegistrarAceptacion(
        dobles.decisiones, dobles.aceptaciones, dobles.reloj, ConfigAceptacion()
    ).ejecutar(
        ComandoAceptacion(
            decision.cotizacion_id,
            decision.id,
            "2026.10.1",
            Declaraciones(True, True, False, True),
            uuid4(),
            ASESOR,
            "corr",
        )
    )
    return aceptacion, decision


def _como_jsonb(fila: dict[str, Any]) -> dict[str, Any]:
    return {k: getattr(v, "obj", v) for k, v in fila.items()}


async def test_la_fila_vuelve_igual_y_la_huella_se_recalcula(dobles: Any, sinteticos: Any) -> None:
    aceptacion, _ = await _aceptada(dobles, sinteticos)
    fila = fila_aceptacion(aceptacion)
    assert list(fila) == list(COLUMNAS)
    leida = aceptacion_de(_como_jsonb(fila))
    assert leida == aceptacion
    assert huella_evidencia(leida) == fila["huella_evidencia"]


class Cursor:
    def __init__(self, filas: list[Any]) -> None:
        self.filas = filas
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any = None) -> Self:
        self.sentencias.append((sql, parametros))
        return self

    async def fetchone(self) -> Any:
        return self.filas[0] if self.filas else None


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


def _repo(filas: list[Any]) -> tuple[AceptacionesPg, Conexion]:
    conexion = Conexion(filas)
    return AceptacionesPg(Pool(conexion)), conexion  # type: ignore[arg-type]


async def test_guardar_inserta_la_aceptacion_y_su_evento_en_una_transaccion(
    dobles: Any, sinteticos: Any
) -> None:
    aceptacion, decision = await _aceptada(dobles, sinteticos)
    repo, conexion = _repo([(aceptacion.id,)])
    assert await repo.guardar(aceptacion, decision) is True
    (insertar, _), (outbox, parametros) = conexion.cursor_.sentencias
    assert "INSERT INTO aceptaciones" in insertar
    assert "ON CONFLICT DO NOTHING RETURNING id" in insertar
    assert "INSERT INTO outbox" in outbox
    assert parametros[1:4] == (aceptacion.id, "OfertaAceptada", 1)
    assert parametros[5] == "corr"
    assert conexion.transacciones == 1


async def test_guardar_no_escribe_el_evento_si_otra_gano(dobles: Any, sinteticos: Any) -> None:
    aceptacion, decision = await _aceptada(dobles, sinteticos)
    repo, conexion = _repo([])
    assert await repo.guardar(aceptacion, decision) is False
    assert len(conexion.cursor_.sentencias) == 1


async def test_lecturas(dobles: Any, sinteticos: Any) -> None:
    aceptacion, _ = await _aceptada(dobles, sinteticos)
    repo, conexion = _repo([_como_jsonb(fila_aceptacion(aceptacion))])
    assert await repo.por_cotizacion(aceptacion.cotizacion_id) == aceptacion
    assert await repo.por_idempotencia(aceptacion.idempotency_key) == aceptacion
    sql = [s for s, _ in conexion.cursor_.sentencias]
    assert sql[0].endswith("WHERE cotizacion_id = %s")
    assert sql[1].endswith("WHERE idempotency_key = %s")
    vacio, _ = _repo([])
    assert await vacio.por_cotizacion(uuid4()) is None


def test_la_fila_de_una_aceptacion_sin_oferta() -> None:
    instante = datetime(2026, 10, 24, tzinfo=UTC)
    aceptacion = Aceptacion(
        id=uuid4(),
        cotizacion_id=uuid4(),
        oferta_id=None,
        decision_id=uuid4(),
        cliente_id=uuid4(),
        huella_calculo="sha256:calculo",
        vigente_hasta=instante,
        version_declaraciones="2026.10.1",
        declaraciones=Declaraciones(True, True, True, True),
        canal=CanalAceptacion.PORTAL_ASESOR,
        actor_id="a",
        aceptada_en=instante,
        idempotency_key=uuid4(),
        huella_solicitud="s",
        huella_evidencia="sha256:" + "0" * 64,
        correlation_id="c",
    )
    assert aceptacion_de(_como_jsonb(fila_aceptacion(aceptacion))) == aceptacion
