"""Router de lectura de pólizas y su adaptador PostgreSQL, sin base: casos y pool simulados."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from consulta.adapters.outbound.lectura_pg import LecturaPolizasPg
from consulta.adapters.outbound.polizas_pg import COLUMNAS, fila_de
from consulta.application.lectura.casos import ConsultarPoliza, ListarPolizas
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada, Vigencia
from consulta.main import crear_app
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
ACTOR = {"X-Actor-Id": "sub-1", "X-Actor-Rol": "CLIENTE"}


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


def _poliza(cliente_id: UUID) -> PolizaProyectada:
    return PolizaProyectada(
        poliza_id=uuid4(),
        cliente_id=cliente_id,
        numero="100-2026-000001",
        estado="ACTIVA",
        producto_id="vida-deudor",
        producto_version="1.0.0",
        producto_nombre="Vida deudor",
        ramo="VIDA",
        vigencia=Vigencia(date(2026, 10, 25), date(2027, 10, 24)),
        prima=Decimal("64638.10"),
        suma_asegurada=Decimal(50000000),
        moneda="COP",
        periodicidad="MENSUAL",
        coberturas=(CoberturaProyectada("MUERTE", "Muerte", True),),
        documento_huella="sha256:" + "a" * 64,
        firma_id="FIRMA-0001",
        emitida_en=AHORA,
        proyectada_en=AHORA,
    )


class LecturaEnMemoria:
    def __init__(self, *polizas: PolizaProyectada) -> None:
        self.polizas = polizas

    async def por_id(self, poliza_id: UUID) -> PolizaProyectada | None:
        return next((p for p in self.polizas if p.poliza_id == poliza_id), None)

    async def por_cliente(self, cliente_id: UUID) -> list[PolizaProyectada]:
        return [p for p in self.polizas if p.cliente_id == cliente_id]


def _cliente(*polizas: PolizaProyectada) -> TestClient:
    app = crear_app()
    lectura = LecturaEnMemoria(*polizas)
    app.state.listar_polizas = ListarPolizas(lectura, RelojFijo())
    app.state.consultar_poliza = ConsultarPoliza(lectura, RelojFijo())
    return TestClient(app)


def test_detalle_propio_ajeno_e_inexistente() -> None:
    dueno = uuid4()
    p = _poliza(dueno)
    cliente = _cliente(p)

    propia = cliente.get(f"/v1/clientes/{dueno}/polizas/{p.poliza_id}", headers=ACTOR)
    ajena = cliente.get(f"/v1/clientes/{uuid4()}/polizas/{p.poliza_id}", headers=ACTOR)
    inexistente = cliente.get(f"/v1/clientes/{dueno}/polizas/{uuid4()}", headers=ACTOR)

    assert propia.status_code == 200
    assert propia.json()["vigente"] is True
    assert (ajena.status_code, ajena.json()["codigo"]) == (403, "POLIZA_NO_AUTORIZADA")
    assert "100-2026-000001" not in ajena.text
    assert (inexistente.status_code, inexistente.json()["codigo"]) == (404, "POLIZA_NO_ENCONTRADA")


def test_lista_y_actor_requerido() -> None:
    dueno = uuid4()
    cliente = _cliente(_poliza(dueno))

    lista = cliente.get(f"/v1/clientes/{dueno}/polizas", headers=ACTOR)
    sin_actor = cliente.get(f"/v1/clientes/{dueno}/polizas")
    otro_rol = cliente.get(
        f"/v1/clientes/{dueno}/polizas", headers={"X-Actor-Id": "a", "X-Actor-Rol": "ASESOR"}
    )

    assert [p["numero"] for p in lista.json()] == ["100-2026-000001"]
    assert sin_actor.status_code == 401
    assert otro_rol.status_code == 403


class _Cursor:
    def __init__(self, filas: list[tuple[Any, ...]]) -> None:
        self._filas = filas

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._filas[0] if self._filas else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._filas


class _Conexion:
    def __init__(self, filas: list[tuple[Any, ...]]) -> None:
        self._filas = filas
        self.consultas: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self, **_: Any) -> "_Conexion":
        return self

    async def execute(self, sql: str, parametros: tuple[Any, ...]) -> _Cursor:
        self.consultas.append((sql, parametros))
        return _Cursor(self._filas)


class _Pool:
    def __init__(self, *filas: tuple[Any, ...]) -> None:
        self.conexion = _Conexion(list(filas))

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[_Conexion]:
        yield self.conexion


def _fila(p: PolizaProyectada) -> tuple[Any, ...]:
    fila = fila_de(p)
    fila[COLUMNAS.index("coberturas")] = [
        {"codigo": c.codigo, "nombre": c.nombre, "obligatoria": c.obligatoria} for c in p.coberturas
    ]
    return tuple(fila)


async def test_el_adaptador_reconstruye_la_poliza_y_ordena_por_vigencia_descendente() -> None:
    p = _poliza(uuid4())
    pool = _Pool(_fila(p))
    lectura = LecturaPolizasPg(cast("AsyncConnectionPool[Any]", pool))

    assert await lectura.por_id(p.poliza_id) == p
    assert await lectura.por_cliente(p.cliente_id) == [p]
    assert "ORDER BY vigencia_hasta DESC" in pool.conexion.consultas[-1][0]


def _sin_filas() -> LecturaPolizasPg:
    return LecturaPolizasPg(cast("AsyncConnectionPool[Any]", _Pool()))


async def test_el_adaptador_devuelve_none_si_no_hay_fila() -> None:
    assert await _sin_filas().por_id(uuid4()) is None
