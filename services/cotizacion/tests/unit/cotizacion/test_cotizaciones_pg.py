"""CotizacionesPg (HU-W03 y HU-W04) con un pool falso: la fila conserva todo y vuelve igual."""

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest
from cotizacion.adapters.outbound.cotizaciones_pg import (
    COLUMNAS,
    COLUMNAS_OFERTA,
    CotizacionesPg,
    cotizacion_de,
    fila_cotizacion,
    fila_oferta,
)
from cotizacion.domain.perfilamiento.errores import ConflictoIdempotencia

pytestmark = pytest.mark.anyio


class Cursor:
    def __init__(self, fila: dict[str, Any] | None) -> None:
        self.fila = fila
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any) -> None:
        self.sentencias.append((sql, parametros))

    async def fetchone(self) -> Any:
        # El RETURNING se lee como tupla; las lecturas, con dict_row.
        if self.fila is not None and set(self.fila) == {"codigo"}:
            return (self.fila["codigo"],)
        return self.fila


class Conexion:
    def __init__(
        self,
        fila: dict[str, Any] | None = None,
        error: Exception | None = None,
        codigo: str = "COT-2026-00001",
    ) -> None:
        self.cursor_ = Cursor(fila)
        self.error = error
        self.codigo = codigo
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any) -> Cursor:
        self.sentencias.append((sql, parametros))
        if self.error:
            raise self.error
        return Cursor({"codigo": self.codigo} if "RETURNING codigo" in sql else None)

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


def _decodificada(fila: dict[str, Any]) -> dict[str, Any]:
    """Lo que devuelve psycopg: los jsonb ya decodificados."""
    return {k: (v.obj if hasattr(v, "obj") else v) for k, v in fila.items()}


def _leida(cotizacion: Any) -> dict[str, Any]:
    """La fila del LEFT JOIN: la cotización y su oferta (columnas `oferta_*`, nulas si no hay)."""
    fila = _decodificada(fila_cotizacion(cotizacion))
    oferta = cotizacion.oferta
    if oferta is None:
        return fila | {f"oferta_{c}": None for c in (*COLUMNAS_OFERTA, "codigo")}
    columnas = _decodificada(fila_oferta(oferta))
    # numeric(20,4): la base devuelve el precio con cuatro decimales.
    columnas["precio"] = oferta.precio.quantize(Decimal("0.0001"))
    return fila | {f"oferta_{c}": v for c, v in columnas.items()} | {"oferta_codigo": oferta.codigo}


async def test_la_fila_vuelve_igual(cotizacion: Any) -> None:
    fila = fila_cotizacion(cotizacion)
    assert set(fila) == set(COLUMNAS)
    assert fila["nivel_riesgo"] == cotizacion.entradas.nivel_riesgo.value
    assert fila["entradas"].obj["sumaAsegurada"] == "180000000"
    assert "fechaNacimiento" not in fila["entradas"].obj
    assert fila["prima_total"] == cotizacion.desglose.prima_total
    leida = cotizacion_de(_leida(cotizacion))
    assert leida == cotizacion
    assert str(leida.oferta.precio) == str(cotizacion.desglose.prima_total)


async def test_la_oferta_conserva_coberturas_exclusiones_y_vigencia(
    escenario_cotizacion: Any, nueva_cotizacion: Any, semilla_w03: Any
) -> None:
    _, reglas_semilla = semilla_w03
    escenario_cotizacion.productos.reglas["vida-hipotecario"] = reglas_semilla(
        indice=-1, vigente_desde=escenario_cotizacion.reloj.ahora()
    )
    calculada, _, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(coberturas_opcionales=("ENFERMEDADES_GRAVES",))
    )
    columnas = fila_oferta(calculada.oferta)
    assert set(columnas) == set(COLUMNAS_OFERTA)
    assert columnas["coberturas"].obj[-1] == {
        "codigo": "ENFERMEDADES_GRAVES",
        "nombre": "Enfermedades graves",
        "sumaAsegurada": "54000000",
    }
    assert len(columnas["exclusiones"].obj) == 4
    assert cotizacion_de(_leida(calculada)) == calculada


async def test_sin_oferta_guardada_vuelve_sin_oferta(cotizacion: Any) -> None:
    anterior = dataclasses.replace(cotizacion, oferta=None)
    assert cotizacion_de(_leida(anterior)).oferta is None


async def test_sin_nivel_vuelve_sin_nivel(escenario_cotizacion: Any, nueva_cotizacion: Any) -> None:
    calculada, _, _ = await escenario_cotizacion.caso().ejecutar(nueva_cotizacion())
    fila = fila_cotizacion(calculada)
    assert fila["nivel_riesgo"] is None
    assert cotizacion_de(_leida(calculada)).entradas.nivel_riesgo is None


async def test_guardar_y_leer(cotizacion: Any) -> None:
    sin_codigo = dataclasses.replace(
        cotizacion, oferta=dataclasses.replace(cotizacion.oferta, codigo=None)
    )
    conexion = Conexion(_leida(cotizacion), codigo=cotizacion.oferta.codigo)
    repo = CotizacionesPg(Pool(conexion))  # type: ignore[arg-type]
    # La oferta vuelve con el código que asignó la secuencia.
    assert await repo.guardar(sin_codigo) == cotizacion
    (sql, parametros), (sql_oferta, p_oferta) = conexion.sentencias
    assert sql.startswith("INSERT INTO cotizaciones") and len(parametros) == len(COLUMNAS)
    assert "INSERT INTO ofertas" in sql_oferta and "nextval('ofertas_codigo_seq')" in sql_oferta
    # El primer parámetro es el instante del que sale el año del código.
    assert p_oferta[0] == cotizacion.oferta.creada_en
    assert len(p_oferta) == 1 + len(COLUMNAS_OFERTA)
    assert await repo.por_id(cotizacion.id) == cotizacion
    assert await repo.por_idempotencia("asesor-sintetico", cotizacion.idempotency_key) == cotizacion
    (sql1, p1), (sql2, p2) = conexion.cursor_.sentencias
    assert "LEFT JOIN ofertas o ON o.cotizacion_id = c.id" in sql1
    assert p1 == (cotizacion.id,)
    assert "c.actor_id = %s AND c.idempotency_key = %s" in sql2
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


async def test_sin_oferta_solo_inserta_la_cotizacion(cotizacion: Any) -> None:
    conexion = Conexion()
    anterior = dataclasses.replace(cotizacion, oferta=None)
    assert await CotizacionesPg(Pool(conexion)).guardar(anterior) == anterior  # type: ignore[arg-type]
    assert len(conexion.sentencias) == 1
