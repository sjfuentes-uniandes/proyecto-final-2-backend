"""CotizacionesPg (HU-W03) con un pool falso: la fila conserva todo y vuelve igual."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
import pytest
from cotizacion.adapters.outbound.cotizaciones_pg import (
    COLUMNAS,
    CotizacionesPg,
    cotizacion_de,
    fila_cotizacion,
)
from cotizacion.domain.perfilamiento.errores import ConflictoIdempotencia

pytestmark = pytest.mark.anyio


class Cursor:
    def __init__(self, fila: dict[str, Any] | None) -> None:
        self.fila = fila
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any) -> None:
        self.sentencias.append((sql, parametros))

    async def fetchone(self) -> dict[str, Any] | None:
        return self.fila


class Conexion:
    def __init__(self, fila: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.cursor_ = Cursor(fila)
        self.error = error
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any) -> None:
        self.sentencias.append((sql, parametros))
        if self.error:
            raise self.error

    def cursor(self, row_factory: Any) -> Cursor:
        return self.cursor_


class Pool:
    def __init__(self, conexion: Conexion) -> None:
        self.conexion = conexion

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Conexion]:
        yield self.conexion


@pytest.fixture
async def cotizacion(escenario_cotizacion: Any, nueva_cotizacion: Any) -> Any:
    calculada, _, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(
            perfil_id=UUID("00000000-0000-4000-a000-000000000001"),
            socio_id="SOC-000001",
            coberturas_opcionales=("ENFERMEDADES_GRAVES",),
        )
    )
    return calculada


def _leida(fila: dict[str, Any]) -> dict[str, Any]:
    """Lo que devuelve psycopg: los jsonb ya decodificados."""
    return {k: (v.obj if hasattr(v, "obj") else v) for k, v in fila.items()}


async def test_la_fila_vuelve_igual(cotizacion: Any) -> None:
    fila = fila_cotizacion(cotizacion)
    assert set(fila) == set(COLUMNAS)
    assert fila["nivel_riesgo"] == cotizacion.entradas.nivel_riesgo.value
    assert fila["entradas"].obj["sumaAsegurada"] == "180000000"
    assert "fechaNacimiento" not in fila["entradas"].obj
    assert fila["prima_total"] == cotizacion.desglose.prima_total
    assert cotizacion_de(_leida(fila)) == cotizacion


async def test_sin_nivel_vuelve_sin_nivel(escenario_cotizacion: Any, nueva_cotizacion: Any) -> None:
    calculada, _, _ = await escenario_cotizacion.caso().ejecutar(nueva_cotizacion())
    fila = fila_cotizacion(calculada)
    assert fila["nivel_riesgo"] is None
    assert cotizacion_de(_leida(fila)).entradas.nivel_riesgo is None


async def test_guardar_y_leer(cotizacion: Any) -> None:
    conexion = Conexion(_leida(fila_cotizacion(cotizacion)))
    repo = CotizacionesPg(Pool(conexion))  # type: ignore[arg-type]
    await repo.guardar(cotizacion)
    sql, parametros = conexion.sentencias[0]
    assert sql.startswith("INSERT INTO cotizaciones") and len(parametros) == len(COLUMNAS)
    assert await repo.por_id(cotizacion.id) == cotizacion
    assert await repo.por_idempotencia("asesor-sintetico", cotizacion.idempotency_key) == cotizacion
    (_, p1), (sql2, p2) = conexion.cursor_.sentencias
    assert p1 == (cotizacion.id,)
    assert "actor_id = %s AND idempotency_key = %s" in sql2
    assert p2 == ("asesor-sintetico", cotizacion.idempotency_key)
    assert await CotizacionesPg(Pool(Conexion())).por_id(cotizacion.id) is None  # type: ignore[arg-type]


class Diagnostico:
    def __init__(self, restriccion: str) -> None:
        self.constraint_name = restriccion


class Duplicada(psycopg.errors.UniqueViolation):
    def __init__(self, restriccion: str) -> None:
        super().__init__("duplicada")
        self._restriccion = restriccion

    @property
    def diag(self) -> Any:  # type: ignore[override]
        return Diagnostico(self._restriccion)


async def test_la_clave_repetida_es_conflicto_de_idempotencia(cotizacion: Any) -> None:
    repetida = Conexion(error=Duplicada("cotizaciones_actor_id_idempotency_key_key"))
    with pytest.raises(ConflictoIdempotencia):
        await CotizacionesPg(Pool(repetida)).guardar(cotizacion)  # type: ignore[arg-type]
    otra = Conexion(error=Duplicada("cotizaciones_pkey"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        await CotizacionesPg(Pool(otra)).guardar(cotizacion)  # type: ignore[arg-type]
