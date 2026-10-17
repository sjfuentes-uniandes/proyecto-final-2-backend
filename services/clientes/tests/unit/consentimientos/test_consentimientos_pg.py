"""Repositorio de consentimientos y outbox con un pool falso; el SQL real lo cubre tests/module."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from clientes.adapters.outbound import outbox_pg
from clientes.adapters.outbound.consentimientos_pg import RETENCION_CLAVES, ConsentimientosPg
from clientes.application.consentimientos.puertos import Otorgamiento
from clientes.domain.consentimientos.modelos import (
    ClaveIdempotenciaEnUso,
    Consentimiento,
    ConsentimientoActivoExistente,
    EstadoConsentimiento,
    SolicitudOtorgamiento,
)
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.anyio

CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
ID = UUID("00000000-0000-4000-8000-0000000000c1")
BOGOTA = timezone(timedelta(hours=-5))
OTORGADO = datetime(2026, 10, 17, 12, tzinfo=UTC)
VIGENTE_HASTA = datetime(2027, 10, 17, 12, tzinfo=UTC)


def _fila(**cambios: Any) -> dict[str, Any]:
    return {
        "id": ID,
        "cliente_id": CLIENTE,
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": ["DA_REGISTROS_PUBLICOS", "FA_PRODUCTOS_VIGENTES"],
        "version_texto": "v1",
        "idioma": "es-CO",
        "estado": "ACTIVO",
        "otorgado_en": OTORGADO.astimezone(BOGOTA),
        "vigente_hasta": VIGENTE_HASTA.astimezone(BOGOTA),
        "revocado_en": None,
        "ahora": OTORGADO.astimezone(BOGOTA),
    } | cambios


class UniqueViolacionFalsa(psycopg.errors.UniqueViolation):
    def __init__(self, restriccion: str) -> None:
        super().__init__("duplicado")
        self._restriccion = restriccion

    @property
    def diag(self) -> Any:
        return SimpleNamespace(constraint_name=self._restriccion)


class CursorFalso:
    def __init__(self, conexion: "ConexionFalsa") -> None:
        self._conexion = conexion

    async def execute(self, sql: str, parametros: Any = None) -> None:
        await self._conexion.execute(sql, parametros)

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._conexion.filas

    async def fetchone(self) -> dict[str, Any] | None:
        return self._conexion.filas[0] if self._conexion.filas else None


class ConexionFalsa:
    def __init__(self, filas: list[dict[str, Any]]) -> None:
        self.filas = filas
        self.sentencias: list[tuple[str, Any]] = []
        self.fallas: dict[str, Exception] = {}
        self.transacciones = 0

    async def execute(self, sql: str, parametros: Any = None) -> None:
        self.sentencias.append((sql, parametros))
        for fragmento, error in self.fallas.items():
            if fragmento in sql:
                raise error

    def cursor(self, **_: Any) -> CursorFalso:
        return CursorFalso(self)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.transacciones += 1
        yield


class PoolFalso:
    def __init__(self, conexion: ConexionFalsa) -> None:
        self.conexion = conexion

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[ConexionFalsa]:
        yield self.conexion


def _repo(*filas: dict[str, Any]) -> tuple[ConsentimientosPg, ConexionFalsa]:
    conexion = ConexionFalsa(list(filas))
    return ConsentimientosPg(PoolFalso(conexion)), conexion  # type: ignore[arg-type]


def _otorgamiento() -> Otorgamiento:
    return Otorgamiento(
        id=ID,
        cliente_id=CLIENTE,
        solicitud=SolicitudOtorgamiento.desde(
            proposito="PERFILAMIENTO_PRECIO",
            alcances=["FA_PRODUCTOS_VIGENTES", "DA_REGISTROS_PUBLICOS"],
            vigencia_meses=6,
            version_texto="v1",
            idioma="es-CO",
        ),
        clave=UUID("00000000-0000-4000-8000-0000000000aa"),
        huella="huella",
        actor_id="sub-1",
        correlation_id="corr-1",
    )


def _contiene(sql: str, *fragmentos: str) -> bool:
    return all(f in sql for f in fragmentos)


async def test_las_lecturas_convierten_las_marcas_a_utc() -> None:
    repo, conexion = _repo(_fila(revocado_en=OTORGADO.astimezone(BOGOTA), estado="REVOCADO"))
    c = await repo.obtener(CLIENTE, ID)
    assert c is not None
    assert c.estado is EstadoConsentimiento.REVOCADO
    assert c.alcances == ("DA_REGISTROS_PUBLICOS", "FA_PRODUCTOS_VIGENTES")
    for marca in (c.otorgado_en, c.vigente_hasta, c.revocado_en, c.leido_en):
        assert marca is not None
        assert marca.utcoffset() == timedelta(0)
    assert (c.otorgado_en, c.vigente_hasta, c.leido_en) == (OTORGADO, VIGENTE_HASTA, OTORGADO)
    sql, parametros = conexion.sentencias[0]
    assert parametros == (ID, CLIENTE)
    # El reloj se captura una sola vez por sentencia (ADR 0010).
    assert _contiene(sql, "WITH t AS MATERIALIZED (SELECT clock_timestamp() AS ahora)")
    assert "now()" not in sql


async def test_obtener_de_otro_cliente_o_inexistente_es_none() -> None:
    repo, _ = _repo()
    assert await repo.obtener(CLIENTE, ID) is None


async def test_activo_filtra_por_cliente_y_proposito() -> None:
    repo, conexion = _repo(_fila())
    c = await repo.activo(CLIENTE, "PERFILAMIENTO_PRECIO")
    assert c is not None
    assert c.revocado_en is None
    assert conexion.sentencias[0][1] == (CLIENTE, "PERFILAMIENTO_PRECIO")
    assert "estado = 'ACTIVO'" in conexion.sentencias[0][0]
    vacio, _ = _repo()
    assert await vacio.activo(CLIENTE, "PERFILAMIENTO_PRECIO") is None


async def test_listar_mapea_todas_las_filas_del_mas_reciente_al_mas_antiguo() -> None:
    otro = UUID("00000000-0000-4000-8000-0000000000c2")
    repo, conexion = _repo(_fila(), _fila(id=otro))
    assert [c.id for c in await repo.listar(CLIENTE)] == [ID, otro]
    assert "ORDER BY c.otorgado_en DESC" in conexion.sentencias[0][0]
    vacio, _ = _repo()
    assert await vacio.listar(CLIENTE) == []


async def test_por_clave_respeta_la_retencion_de_24_horas() -> None:
    clave = uuid4()
    repo, conexion = _repo({**_fila(), "hash_cuerpo": "h-1"})
    registro = await repo.por_clave(CLIENTE, clave)
    assert registro is not None
    assert (registro.huella, registro.consentimiento.id) == ("h-1", ID)
    assert conexion.sentencias[0][1] == (CLIENTE, clave, RETENCION_CLAVES)
    assert RETENCION_CLAVES == timedelta(hours=24)
    vacio, _ = _repo()
    assert await vacio.por_clave(CLIENTE, clave) is None


async def test_crear_ejecuta_en_una_transaccion_barrido_vencido_dato_clave_y_evento() -> None:
    repo, conexion = _repo(_fila())
    otorgamiento = _otorgamiento()
    creado = await repo.crear(otorgamiento)
    assert creado.id == ID
    assert conexion.transacciones == 1
    sentencias = [sql for sql, _ in conexion.sentencias]
    assert [
        _contiene(sentencias[0], "DELETE FROM consentimiento_idempotencia"),
        _contiene(sentencias[1], "SET estado = 'VENCIDO'", "vigente_hasta <= t.ahora"),
        _contiene(sentencias[2], "INSERT INTO consentimiento ", "fin_de_vigencia(t.ahora"),
        _contiene(sentencias[3], "INSERT INTO consentimiento_idempotencia"),
        _contiene(sentencias[4], "INSERT INTO outbox"),
    ] == [True] * 5
    assert len(sentencias) == 5
    assert conexion.sentencias[1][1] == (CLIENTE, "PERFILAMIENTO_PRECIO")
    assert conexion.sentencias[2][1] == (
        ID,
        CLIENTE,
        "PERFILAMIENTO_PRECIO",
        ["DA_REGISTROS_PUBLICOS", "FA_PRODUCTOS_VIGENTES"],
        "v1",
        "es-CO",
        6,
    )
    # La clave se registra con el mismo instante del otorgamiento.
    assert conexion.sentencias[3][1] == (CLIENTE, otorgamiento.clave, "huella", ID, OTORGADO)


@pytest.mark.parametrize(
    ("restriccion", "esperado"),
    [
        ("consentimiento_activo_unico", ConsentimientoActivoExistente),
        ("consentimiento_idempotencia_pk", ClaveIdempotenciaEnUso),
        ("consentimiento_pkey", UniqueViolacionFalsa),
    ],
)
async def test_crear_traduce_las_violaciones_de_unicidad(
    restriccion: str, esperado: type[Exception]
) -> None:
    repo, conexion = _repo(_fila())
    conexion.fallas["INSERT INTO consentimiento "] = UniqueViolacionFalsa(restriccion)
    with pytest.raises(esperado):
        await repo.crear(_otorgamiento())


async def test_crear_propaga_otros_errores_sin_traducir() -> None:
    repo, conexion = _repo(_fila())
    conexion.fallas["INSERT INTO outbox"] = RuntimeError("outbox caído")
    with pytest.raises(RuntimeError, match="outbox caído"):
        await repo.crear(_otorgamiento())


def _consentimiento() -> Consentimiento:
    return Consentimiento(
        id=ID,
        cliente_id=CLIENTE,
        proposito="PERFILAMIENTO_PRECIO",
        alcances=("DA_REGISTROS_PUBLICOS", "FA_PRODUCTOS_VIGENTES"),
        version_texto="v1",
        idioma="es-CO",
        estado=EstadoConsentimiento.ACTIVO,
        otorgado_en=OTORGADO.astimezone(BOGOTA),
        vigente_hasta=VIGENTE_HASTA,
        revocado_en=None,
        leido_en=OTORGADO,
    )


def test_el_evento_otorgado_lleva_solo_identificadores_y_codigos_en_utc() -> None:
    evento = outbox_pg.consentimiento_otorgado(_consentimiento(), "sub-1", "corr-1")
    assert (evento.event_type, evento.event_version, evento.aggregate_id) == (
        "ConsentimientoOtorgado",
        1,
        ID,
    )
    assert evento.occurred_at == OTORGADO
    assert evento.correlation_id == "corr-1"
    assert evento.payload == {
        "consentimientoId": str(ID),
        "clienteId": str(CLIENTE),
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": ["DA_REGISTROS_PUBLICOS", "FA_PRODUCTOS_VIGENTES"],
        "versionTexto": "v1",
        "otorgadoEn": "2026-10-17T12:00:00+00:00",
        "vigenteHasta": "2027-10-17T12:00:00+00:00",
        "actorId": "sub-1",
    }


def test_los_eventos_tienen_identificador_propio() -> None:
    a = outbox_pg.consentimiento_otorgado(_consentimiento(), "sub-1", "corr-1")
    b = outbox_pg.consentimiento_otorgado(_consentimiento(), "sub-1", "corr-1")
    assert a.event_id != b.event_id


async def test_insertar_escribe_la_fila_sin_publicar() -> None:
    conexion = ConexionFalsa([])
    evento = outbox_pg.consentimiento_otorgado(_consentimiento(), "sub-1", "corr-1")
    await outbox_pg.insertar(conexion, evento)  # type: ignore[arg-type]
    (sql, parametros) = conexion.sentencias[0]
    assert "published_at" not in sql  # nulo por defecto: lo marcará el relay
    assert parametros[:6] == (
        evento.event_id,
        ID,
        "ConsentimientoOtorgado",
        1,
        OTORGADO,
        "corr-1",
    )
    assert isinstance(parametros[6], Jsonb)
