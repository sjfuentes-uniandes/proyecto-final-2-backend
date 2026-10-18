"""Repositorio de perfiles: filas, transacción, linaje, outbox y migraciones (W11-P09).

Las pruebas contra PostgreSQL real están en tests/module (W11-P14).
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID

import psycopg
import pytest
from cotizacion.adapters.outbound import perfiles_pg
from cotizacion.adapters.outbound.perfiles_pg import (
    COLUMNAS_LINAJE,
    COLUMNAS_PERFIL,
    PerfilesPg,
    fila_perfil,
    filas_linaje,
    migrar,
    perfil_de,
)
from cotizacion.domain.perfilamiento.errores import ConflictoIdempotencia
from cotizacion.domain.perfilamiento.modelos import (
    AccionSugerida,
    DatoRequerido,
    DecisionConsentimiento,
    EstadoCampo,
    EstadoFuente,
    EstadoPerfil,
    Fuente,
    MotivoDato,
    Perfil,
    ResultadoEvaluacion,
    ValorCampo,
)
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.anyio


def _como_bd(fila: dict[str, Any]) -> dict[str, Any]:
    """Lo que devuelve psycopg al leer: los jsonb vuelven como JSON decodificado."""
    return {
        k: json.loads(json.dumps(v.obj)) if isinstance(v, Jsonb) else v for k, v in fila.items()
    }


def _ida_y_vuelta(perfil: Perfil) -> Perfil:
    return perfil_de(_como_bd(fila_perfil(perfil)), filas_linaje(perfil))


def _insuficiente(perfil: Perfil) -> Perfil:
    """Variante C08: consentimiento revocado, ninguna fuente consultada."""
    fuentes = tuple(
        replace(
            r,
            estado=EstadoFuente.NO_AUTORIZADA,
            campos_solicitados=(),
            campos={},
            version_fuente=None,
            confianza=None,
            consultada_en=None,
            duracion_ms=None,
            proveedor=None,
        )
        for r in perfil.fuentes
    )
    resultado = ResultadoEvaluacion(
        estado=EstadoPerfil.INFORMACION_INSUFICIENTE,
        nivel=None,
        puntaje=None,
        confianza=Decimal("0.00"),
        factores=(),
        ajustes=(),
        datos_requeridos=(
            DatoRequerido(
                "cuota_mensual_obligaciones", Fuente.FA_PRODUCTOS_VIGENTES, MotivoDato.NO_AUTORIZADO
            ),
            DatoRequerido("moras_12m", Fuente.FA_HISTORIAL_PAGOS_12M, MotivoDato.NO_AUTORIZADO),
        ),
        accion=AccionSugerida.COMPLETAR_CONSENTIMIENTO,
    )
    consentimiento = replace(
        perfil.consentimiento,
        decision=DecisionConsentimiento.RECHAZADO,
        consentimiento_id=None,
        vigente_hasta=None,
        alcances_permitidos=frozenset(),
        causa="REVOCADO",
    )
    insumos = {
        k: v
        for k, v in perfil.insumos.items()
        if k in ("edad", "plazo_credito_meses", "suma_asegurada")
    }
    return replace(
        perfil,
        resultado=resultado,
        fuentes=fuentes,
        consentimiento=consentimiento,
        insumos=insumos,
        perfil_anterior_id=UUID(int=7),
    )


def test_ida_y_vuelta_de_un_perfil_calculado(perfil_c01: Perfil) -> None:
    assert _ida_y_vuelta(perfil_c01) == perfil_c01


def test_ida_y_vuelta_de_un_perfil_insuficiente(perfil_c01: Perfil) -> None:
    perfil = _insuficiente(perfil_c01)
    assert _ida_y_vuelta(perfil) == perfil


def test_ida_y_vuelta_sin_consentimiento_y_con_campo_no_disponible(perfil_c01: Perfil) -> None:
    historial = replace(
        perfil_c01.fuentes[1], campos={"moras_12m": ValorCampo(EstadoCampo.NO_DISPONIBLE, None)}
    )
    insumos = {k: v for k, v in perfil_c01.insumos.items() if k != "moras_12m"}
    perfil = replace(
        perfil_c01,
        consentimiento=None,
        insumos=insumos,
        fuentes=(perfil_c01.fuentes[0], historial, *perfil_c01.fuentes[2:]),
    )
    assert _ida_y_vuelta(perfil) == perfil


def test_las_filas_no_guardan_valores_en_el_linaje(perfil_c01: Perfil) -> None:
    linaje = filas_linaje(perfil_c01)
    assert [f["orden"] for f in linaje] == [0, 1, 2, 3]
    assert linaje[0]["tipo"] == "FINANZAS_ABIERTAS" and linaje[3]["tipo"] == "DATOS_ABIERTOS"
    assert linaje[0]["campos_disponibles"] == list(perfil_c01.fuentes[0].campos_solicitados)
    assert "1800000" not in repr(linaje)
    fila = _como_bd(fila_perfil(perfil_c01))
    assert fila["insumos"]["cuota_mensual_obligaciones"] == "1800000.00"
    assert fila["datos_declarados"] == {
        "fecha_nacimiento": "1990-04-18",
        "plazo_credito_meses": 180,
        "suma_asegurada": "180000000",
    }
    assert fila["factores"][0] == {
        "codigo": "PUNTUALIDAD_PAGOS",
        "fuente": "FA_HISTORIAL_PAGOS_12M",
        "peso": "0.30",
        "estado": "EVALUADO",
        "contribucion": "0.90",
        "direccion": "REDUCE_RIESGO",
        "motivo": None,
    }


# --- Repositorio con conexiones falsas -----------------------------------------------------


class Cursor:
    def __init__(self, resultados: list[list[dict[str, Any]]]) -> None:
        self.resultados = resultados
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any) -> None:
        self.sentencias.append((sql, parametros))

    async def fetchall(self) -> list[dict[str, Any]]:
        return self.resultados.pop(0)


class Conexion:
    def __init__(self, cursor: Cursor | None = None, error: Exception | None = None) -> None:
        self.cursor_ = cursor
        self.error = error
        self.sentencias: list[tuple[str, Any]] = []
        self.transacciones = 0

    async def execute(self, sql: str, parametros: Any = None) -> None:
        self.sentencias.append((sql, parametros))
        if self.error and sql.startswith("INSERT INTO perfiles_riesgo"):
            raise self.error

    def cursor(self, row_factory: Any) -> Cursor:
        assert self.cursor_ is not None
        return self.cursor_

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


def _repo(conexion: Conexion) -> PerfilesPg:
    return PerfilesPg(Pool(conexion))  # type: ignore[arg-type]


async def test_guardar_escribe_perfil_linaje_y_outbox_en_una_transaccion(
    perfil_c01: Perfil,
) -> None:
    conexion = Conexion()
    await _repo(conexion).guardar(perfil_c01)
    tablas = [sql.split()[2] for sql, _ in conexion.sentencias]
    assert tablas == ["perfiles_riesgo"] + ["linaje_fuentes"] * 4 + ["outbox"] * 2
    assert conexion.transacciones == 1
    perfil = conexion.sentencias[0][1]
    assert len(perfil) == len(COLUMNAS_PERFIL) and perfil[0] == perfil_c01.id
    assert [p[1] for _, p in conexion.sentencias[1:5]] == [0, 1, 2, 3]
    tipos = [p[2] for _, p in conexion.sentencias[5:]]
    assert tipos == ["PerfilRiesgoCalculado", "DatosConsentidosConsultados"]
    assert {p[5] for _, p in conexion.sentencias[5:]} == {"corr-c01"}


class _Duplicado(psycopg.errors.UniqueViolation):
    def __init__(self, restriccion: str) -> None:
        super().__init__("duplicado")
        self._restriccion = restriccion

    @property
    def diag(self) -> Any:  # type: ignore[override]
        return SimpleNamespace(constraint_name=self._restriccion)


async def test_clave_de_idempotencia_repetida_es_conflicto(perfil_c01: Perfil) -> None:
    error = _Duplicado("perfiles_riesgo_cliente_id_idempotency_key_key")
    with pytest.raises(ConflictoIdempotencia):
        await _repo(Conexion(error=error)).guardar(perfil_c01)


async def test_otra_violacion_unica_se_propaga(perfil_c01: Perfil) -> None:
    with pytest.raises(psycopg.errors.UniqueViolation):
        await _repo(Conexion(error=_Duplicado("perfiles_riesgo_pkey"))).guardar(perfil_c01)


async def test_lecturas_reconstruyen_con_su_linaje(perfil_c01: Perfil) -> None:
    otro = _insuficiente(replace(perfil_c01, id=UUID(int=2)))
    filas = [_como_bd(fila_perfil(p)) for p in (perfil_c01, otro)]
    linajes = [*filas_linaje(otro), *filas_linaje(perfil_c01)]
    cursor = Cursor([filas, linajes])
    repo = _repo(Conexion(cursor))
    assert await repo.por_cliente(perfil_c01.cliente_id, 20) == [perfil_c01, otro]
    (sql, parametros), (sql_linaje, ids) = cursor.sentencias
    assert "ORDER BY calculado_en DESC" in sql and parametros == (perfil_c01.cliente_id, 20)
    assert "linaje_fuentes" in sql_linaje and ids == ([perfil_c01.id, otro.id],)


@pytest.mark.parametrize("metodo", ["por_id", "por_idempotencia"])
async def test_lectura_por_clave(perfil_c01: Perfil, metodo: str) -> None:
    cursor = Cursor([[_como_bd(fila_perfil(perfil_c01))], filas_linaje(perfil_c01)])
    repo = _repo(Conexion(cursor))
    argumentos = (
        (perfil_c01.id,)
        if metodo == "por_id"
        else (perfil_c01.cliente_id, perfil_c01.idempotency_key)
    )
    assert await getattr(repo, metodo)(*argumentos) == perfil_c01
    assert cursor.sentencias[0][1] == argumentos


@pytest.mark.parametrize("metodo", ["por_id", "por_idempotencia", "por_cliente"])
async def test_sin_filas_no_consulta_el_linaje(metodo: str) -> None:
    cursor = Cursor([[]])
    repo = _repo(Conexion(cursor))
    argumentos = {"por_id": (UUID(int=1),), "por_idempotencia": (UUID(int=1), UUID(int=2))}
    resultado = await getattr(repo, metodo)(*argumentos.get(metodo, (UUID(int=1), 5)))
    assert resultado in (None, [])
    assert len(cursor.sentencias) == 1


# --- migrar --------------------------------------------------------------------------------


class ConexionSincrona:
    def __init__(self, falla_en: str | None = None) -> None:
        self.falla_en = falla_en
        self.sentencias: list[str] = []
        self.argumentos: dict[str, Any] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.sentencias.append(sql)
        if self.falla_en and self.falla_en in sql:
            raise psycopg.errors.SyntaxError("falla")


def test_migrar_bloquea_aplica_y_libera(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = ConexionSincrona()

    def conectar(url: str, **argumentos: Any) -> ConexionSincrona:
        conexion.argumentos = {"url": url, **argumentos}
        return conexion

    monkeypatch.setattr(perfiles_pg.psycopg, "connect", conectar)
    migrar("postgresql://u@h/cotizacion")
    assert conexion.argumentos == {"url": "postgresql://u@h/cotizacion", "autocommit": True}
    assert "pg_advisory_lock(hashtext('cotizacion.migraciones'))" in conexion.sentencias[0]
    assert "CREATE TABLE IF NOT EXISTS perfiles_riesgo" in conexion.sentencias[1]
    assert "CREATE TABLE IF NOT EXISTS outbox" in conexion.sentencias[1]
    assert "pg_advisory_unlock" in conexion.sentencias[-1]


def test_migrar_libera_el_lock_si_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = ConexionSincrona(falla_en="CREATE TABLE")
    monkeypatch.setattr(perfiles_pg.psycopg, "connect", lambda url, **_: conexion)
    with pytest.raises(psycopg.errors.SyntaxError):
        migrar("postgresql://u@h/cotizacion")
    assert "pg_advisory_unlock" in conexion.sentencias[-1]


def test_columnas_del_linaje_coinciden_con_las_filas(perfil_c01: Perfil) -> None:
    assert set(filas_linaje(perfil_c01)[0]) == set(COLUMNAS_LINAJE)
    assert set(fila_perfil(perfil_c01)) == set(COLUMNAS_PERFIL)
    assert perfil_c01.calculado_en == datetime(2026, 10, 15, 15, 0, 0, 212000, tzinfo=UTC)


@pytest.mark.parametrize("causa", [None, "REVOCADO"], ids=["vigente", "revocado"])
def test_ida_y_vuelta_con_comprobacion_final(
    perfil_c01: Perfil, verificacion_de: Any, causa: str | None
) -> None:
    decision = DecisionConsentimiento.RECHAZADO if causa else DecisionConsentimiento.PERMITIDO
    perfil = replace(perfil_c01, comprobacion_final=verificacion_de(decision, causa=causa))
    assert _ida_y_vuelta(perfil) == perfil
    assert _ida_y_vuelta(perfil).revocado_durante_perfilamiento is (causa is not None)
    fila = fila_perfil(perfil)["comprobacion_final"].obj
    assert set(fila) == {
        "decision",
        "consentimiento_id",
        "vigente_hasta",
        "alcances_permitidos",
        "causa",
    }
    assert (fila["decision"], fila["causa"]) == (decision.value, causa)


def test_sin_comprobacion_final_la_columna_es_nula(perfil_c01: Perfil) -> None:
    assert fila_perfil(perfil_c01)["comprobacion_final"] is None
    assert _ida_y_vuelta(perfil_c01).comprobacion_final is None
