"""LecturaEstadoPolizasPg sin base: conexión falsa que registra el SQL y los parámetros."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from consulta.adapters.outbound.estado_pg import LecturaEstadoPolizasPg
from consulta.adapters.outbound.polizas_pg import COLUMNAS, fila_de
from consulta.application.estado.puertos import FiltroPolizas
from consulta.domain.polizas.estado import EstadoVigencia
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada, Vigencia
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
HOY = date(2026, 10, 25)


def _poliza() -> PolizaProyectada:
    return PolizaProyectada(
        poliza_id=uuid4(),
        cliente_id=uuid4(),
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
        documento_huella="h",
        firma_id="f",
        emitida_en=AHORA,
        proyectada_en=AHORA,
    )


def _fila(p: PolizaProyectada) -> tuple[Any, ...]:
    fila = fila_de(p)
    fila[COLUMNAS.index("coberturas")] = [
        {"codigo": c.codigo, "nombre": c.nombre, "obligatoria": c.obligatoria} for c in p.coberturas
    ]
    return tuple(fila)


class _Cursor:
    def __init__(self, filas: list[tuple[Any, ...]]) -> None:
        self._filas = filas

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._filas[0] if self._filas else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._filas


class _Conexion:
    """Responde a cada `execute` con la siguiente lista de filas."""

    def __init__(self, *respuestas: list[tuple[Any, ...]]) -> None:
        self._respuestas = list(respuestas)
        self.consultas: list[tuple[str, Any]] = []

    def cursor(self, **_: Any) -> "_Conexion":
        return self

    async def execute(self, sql: str, parametros: Any) -> _Cursor:
        self.consultas.append((sql, parametros))
        return _Cursor(self._respuestas.pop(0))


class _Pool:
    def __init__(self, *respuestas: list[tuple[Any, ...]]) -> None:
        self.conexion = _Conexion(*respuestas)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[_Conexion]:
        yield self.conexion


def _lectura(*respuestas: list[tuple[Any, ...]]) -> tuple[LecturaEstadoPolizasPg, _Conexion]:
    pool = _Pool(*respuestas)
    return LecturaEstadoPolizasPg(cast("AsyncConnectionPool[Any]", pool)), pool.conexion


async def test_por_id_reconstruye_la_poliza_o_devuelve_none() -> None:
    p = _poliza()
    lectura, conexion = _lectura([_fila(p)], [])

    assert await lectura.por_id(p.poliza_id) == p
    assert await lectura.por_id(uuid4()) is None
    assert "WHERE poliza_id = %s" in conexion.consultas[0][0]


async def test_buscar_sin_filtros_pagina_ordena_y_resume() -> None:
    p = _poliza()
    lectura, conexion = _lectura([(3, AHORA)], [_fila(p)])

    r = await lectura.buscar(FiltroPolizas(HOY, 30), pagina=3, tamano=20)

    assert (r.polizas, r.total, r.actualizado_en) == ((p,), 3, AHORA)
    (total_sql, _), (pagina_sql, parametros) = conexion.consultas
    assert "WHERE" not in total_sql and "WHERE" not in pagina_sql
    assert "max(proyectada_en) FROM poliza_cliente)" in total_sql
    assert (
        "ORDER BY emitida_en DESC, poliza_id LIMIT %(limite)s OFFSET %(desplazamiento)s"
        in pagina_sql
    )
    assert (parametros["limite"], parametros["desplazamiento"]) == (20, 40)


async def test_buscar_sin_resumen_devuelve_total_cero() -> None:
    lectura, _ = _lectura([], [])

    r = await lectura.buscar(FiltroPolizas(HOY, 30), pagina=1, tamano=20)

    assert (r.polizas, r.total, r.actualizado_en) == ((), 0, None)


async def test_los_valores_viajan_como_parametros_y_no_en_el_sql() -> None:
    lectura, conexion = _lectura([(0, None)], [])
    inyeccion = "VIDA'; DROP TABLE poliza_cliente; --"
    filtro = FiltroPolizas(
        HOY, 30, "100-2026-000001", inyeccion, EstadoVigencia.VIGENTE, date(2026, 11, 1)
    )

    await lectura.buscar(filtro, 1, 20)

    for sql, parametros in conexion.consultas:
        assert inyeccion not in sql and "100-2026-000001" not in sql
        assert parametros["ramo"] == inyeccion
        assert parametros["numero"] == "100-2026-000001"
        assert parametros["vigencia_desde"] == date(2026, 11, 1)
        assert "numero = %(numero)s AND ramo = %(ramo)s" in sql


@pytest.mark.parametrize(
    ("estado", "fragmento"),
    [
        (EstadoVigencia.NO_INICIADA, "vigencia_desde > %(hoy)s"),
        (EstadoVigencia.VENCIDA, "vigencia_hasta < %(hoy)s"),
        (EstadoVigencia.POR_VENCER, "- %(hoy)s::date) <= %(dias)s"),
        (EstadoVigencia.VIGENTE, "- %(hoy)s::date) > %(dias)s"),
    ],
)
async def test_el_filtro_de_estado_se_evalua_en_sql(estado: EstadoVigencia, fragmento: str) -> None:
    lectura, conexion = _lectura([(0, None)], [])

    await lectura.buscar(FiltroPolizas(HOY, 15, estado=estado), 1, 20)

    sql, parametros = conexion.consultas[0]
    assert fragmento in sql
    assert (parametros["hoy"], parametros["dias"]) == (HOY, 15)
