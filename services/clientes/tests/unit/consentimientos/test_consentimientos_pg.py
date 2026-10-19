"""Repositorio de consentimientos y outbox con un pool falso; el SQL real lo cubre tests/module."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from clientes.adapters.outbound import consentimientos_pg, outbox_pg
from clientes.adapters.outbound.consentimientos_pg import (
    RETENCION_CLAVES,
    ConsentimientosPg,
    lectura_verificacion,
)
from clientes.application.consentimientos.puertos import Otorgamiento, Revocacion
from clientes.domain.consentimientos.modelos import (
    ClaveIdempotenciaEnUso,
    Consentimiento,
    ConsentimientoActivoExistente,
    EstadoConsentimiento,
    SolicitudOtorgamiento,
)
from clientes.domain.consentimientos.verificacion import (
    CausaRechazo,
    Decision,
    SolicitudVerificacion,
    decidir,
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
        return self._conexion.ultimas

    async def fetchone(self) -> dict[str, Any] | None:
        return self._conexion.ultimas[0] if self._conexion.ultimas else None


class ConexionFalsa:
    def __init__(self, filas: list[dict[str, Any]]) -> None:
        self.filas = filas
        # Filas propias de las sentencias que contienen el fragmento; las demás devuelven `filas`.
        self.respuestas: dict[str, list[dict[str, Any]]] = {}
        self.ultimas = filas
        self.sentencias: list[tuple[str, Any]] = []
        self.fallas: dict[str, Exception] = {}
        self.transacciones = 0
        self.en_transaccion: list[bool] = []
        self._abierta = False

    async def execute(self, sql: str, parametros: Any = None) -> None:
        self.sentencias.append((sql, parametros))
        self.en_transaccion.append(self._abierta)
        for fragmento, error in self.fallas.items():
            if fragmento in sql:
                raise error
        self.ultimas = next(
            (filas for fragmento, filas in self.respuestas.items() if fragmento in sql),
            self.filas,
        )

    def cursor(self, **_: Any) -> CursorFalso:
        return CursorFalso(self)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.transacciones += 1
        self._abierta = True
        try:
            yield
        finally:
            self._abierta = False


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


def _revocacion() -> Revocacion:
    return Revocacion(
        consentimiento_id=ID, cliente_id=CLIENTE, actor_id="sub-1", correlation_id="corr-1"
    )


async def test_revocar_es_una_sentencia_con_una_captura_y_el_evento_en_su_transaccion() -> None:
    repo, conexion = _repo(_fila(estado="REVOCADO", revocado_en=OTORGADO, ahora=OTORGADO))
    revocado = await repo.revocar(_revocacion())
    assert revocado is not None
    assert (revocado.estado, revocado.revocado_en) == (EstadoConsentimiento.REVOCADO, OTORGADO)
    assert conexion.transacciones == 1
    (sql, parametros), (sql_outbox, evento) = conexion.sentencias
    assert parametros == (ID, CLIENTE)
    # ADR 0009 y 0010: una sola sentencia, una sola captura del reloj para SET, WHERE y RETURNING.
    assert sql.count("clock_timestamp()") == 1
    assert "now()" not in sql
    assert _contiene(
        sql,
        "WITH t AS MATERIALIZED (SELECT clock_timestamp() AS ahora)",
        "SET estado = 'REVOCADO', revocado_en = t.ahora",
        "c.estado = 'ACTIVO' AND c.vigente_hasta > t.ahora",
        "c.id = %s AND c.cliente_id = %s",
        "RETURNING",
    )
    assert "INSERT INTO outbox" in sql_outbox
    assert evento[2:6] == ("ConsentimientoRevocado", 1, OTORGADO, "corr-1")
    assert conexion.en_transaccion == [True, True]


@pytest.mark.parametrize(
    ("estado", "revocado_en"),
    [("REVOCADO", OTORGADO), ("VENCIDO", None), ("ACTIVO", None)],
)
async def test_revocar_sin_filas_afectadas_devuelve_lo_que_hay_sin_evento(
    estado: str, revocado_en: datetime | None
) -> None:
    """Ya revocado, vencido materializado o activo vencido sin materializar: sin evento."""
    repo, conexion = _repo(_fila(estado=estado, revocado_en=revocado_en))
    conexion.respuestas["UPDATE consentimiento"] = []
    visto = await repo.revocar(_revocacion())
    assert visto is not None
    assert visto.estado is EstadoConsentimiento(estado)
    sentencias = [sql for sql, _ in conexion.sentencias]
    assert len(sentencias) == 2
    assert "UPDATE consentimiento" in sentencias[0]
    assert _contiene(sentencias[1], "SELECT", "WHERE c.id = %s AND c.cliente_id = %s")
    assert conexion.sentencias[1][1] == (ID, CLIENTE)
    assert not any("outbox" in sql for sql in sentencias)
    # La relectura va fuera de la transacción ya cerrada: no retiene el bloqueo.
    assert conexion.en_transaccion == [True, False]


async def test_revocar_uno_ajeno_o_inexistente_es_none() -> None:
    repo, _ = _repo()
    assert await repo.revocar(_revocacion()) is None


async def test_revocar_propaga_el_fallo_del_outbox() -> None:
    repo, conexion = _repo(_fila(estado="REVOCADO", revocado_en=OTORGADO))
    conexion.fallas["INSERT INTO outbox"] = RuntimeError("outbox caído")
    with pytest.raises(RuntimeError, match="outbox caído"):
        await repo.revocar(_revocacion())


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


def test_el_evento_revocado_lleva_solo_identificadores_y_la_fecha_en_utc() -> None:
    revocado_en = datetime(2026, 11, 2, 8, 15, tzinfo=UTC)
    consentimiento = replace(
        _consentimiento(),
        estado=EstadoConsentimiento.REVOCADO,
        revocado_en=revocado_en.astimezone(BOGOTA),
    )
    evento = outbox_pg.consentimiento_revocado(consentimiento, "sub-1", "corr-1")
    assert (evento.event_type, evento.event_version, evento.aggregate_id) == (
        "ConsentimientoRevocado",
        1,
        ID,
    )
    assert evento.occurred_at == revocado_en
    assert evento.correlation_id == "corr-1"
    assert evento.payload == {
        "consentimientoId": str(ID),
        "clienteId": str(CLIENTE),
        "revocadoEn": "2026-11-02T08:15:00+00:00",
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


# Verificación (HU-W31, PR 3.2): una consulta con el reloj atómico y el evento en la transacción.
CUATRO = (
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
)
VERIFICACION = SolicitudVerificacion.crear("PERFILAMIENTO_PRECIO", CUATRO)
_SIN_CONSENTIMIENTO = dict.fromkeys(
    (
        "id",
        "cliente_id",
        "proposito",
        "alcances",
        "version_texto",
        "idioma",
        "estado",
        "otorgado_en",
        "vigente_hasta",
        "revocado_en",
    )
)


def _fila_verificacion(
    consentimiento: dict[str, Any] | None = None, otros: list[datetime] | None = None
) -> dict[str, Any]:
    """Como llega de `_VERIFICAR`: sin consentimiento del propósito, sus columnas son nulas."""
    ahora = OTORGADO + timedelta(days=1)
    columnas = _fila(ahora=ahora.astimezone(BOGOTA)) | (consentimiento or {})
    if consentimiento is None:
        columnas |= _SIN_CONSENTIMIENTO
    return columnas | {"cliente_id_consultado": CLIENTE, "otros_activos_hasta": otros or []}


def test_la_consulta_de_verificacion_captura_el_reloj_una_vez_y_no_decide_la_vigencia() -> None:
    sql = consentimientos_pg._VERIFICAR
    assert sql.startswith("WITH t AS MATERIALIZED (SELECT clock_timestamp() AS ahora) ")
    assert sql.count("clock_timestamp()") == 1
    assert "now()" not in sql
    assert "vigente_hasta >" not in sql and "vigente_hasta <" not in sql  # la decide el dominio
    assert "ORDER BY u.otorgado_en DESC LIMIT 1" in sql


def test_lectura_de_verificacion_en_utc_y_con_otros_propositos() -> None:
    otro = VIGENTE_HASTA.astimezone(BOGOTA)

    lectura = lectura_verificacion(_fila_verificacion({}, otros=[otro]))

    assert lectura.cliente_id == CLIENTE
    assert lectura.ahora == OTORGADO + timedelta(days=1)
    assert lectura.ahora.tzinfo is UTC
    assert lectura.consentimiento is not None and lectura.consentimiento.id == ID
    assert lectura.otros_activos_hasta == (VIGENTE_HASTA,)
    assert lectura.otros_activos_hasta[0].tzinfo is UTC


def test_lectura_de_verificacion_sin_consentimiento_del_proposito() -> None:
    lectura = lectura_verificacion(_fila_verificacion())

    assert (lectura.consentimiento, lectura.otros_activos_hasta) == (None, ())


async def test_verificar_lee_decide_y_registra_en_la_misma_transaccion() -> None:
    repo, conexion = _repo(_fila_verificacion({"alcances": list(CUATRO)}))

    verificacion = await repo.verificar(CLIENTE, VERIFICACION, "corr-w31")

    assert verificacion is not None
    assert verificacion.decision is Decision.PERMITIDO
    assert conexion.transacciones == 1
    (lectura, _), (insercion, parametros) = conexion.sentencias
    assert "FROM cliente cl" in lectura and insercion.startswith("INSERT INTO outbox")
    assert conexion.en_transaccion == [True, True]
    assert conexion.sentencias[0][1] == {"cliente": CLIENTE, "proposito": "PERFILAMIENTO_PRECIO"}
    assert parametros[1:6] == (
        CLIENTE,
        "VerificacionConsentimientoRegistrada",
        1,
        OTORGADO + timedelta(days=1),
        "corr-w31",
    )
    assert parametros[6].obj["decision"] == "PERMITIDO"


async def test_verificar_un_cliente_inexistente_no_escribe_evento() -> None:
    repo, conexion = _repo()

    assert await repo.verificar(CLIENTE, VERIFICACION, "corr-w31") is None
    assert len(conexion.sentencias) == 1  # solo la lectura


async def test_verificar_sin_consentimiento_registra_el_rechazo() -> None:
    repo, conexion = _repo(_fila_verificacion())

    verificacion = await repo.verificar(CLIENTE, VERIFICACION, "corr-w31")

    assert verificacion is not None
    assert (verificacion.decision, verificacion.causa) == (
        Decision.RECHAZADO,
        CausaRechazo.SIN_CONSENTIMIENTO,
    )
    assert conexion.sentencias[1][1][6].obj["causa"] == "SIN_CONSENTIMIENTO"


async def test_si_falla_el_outbox_la_verificacion_falla() -> None:
    repo, conexion = _repo(_fila_verificacion({}))
    conexion.fallas["INSERT INTO outbox"] = psycopg.OperationalError("caída")

    with pytest.raises(psycopg.OperationalError):
        await repo.verificar(CLIENTE, VERIFICACION, "corr-w31")


def test_el_evento_de_verificacion_lleva_solo_identificadores_y_codigos() -> None:
    lectura = lectura_verificacion(
        _fila_verificacion({"estado": "REVOCADO", "revocado_en": OTORGADO.astimezone(BOGOTA)})
    )
    verificacion = decidir(VERIFICACION, lectura)
    evento = outbox_pg.verificacion_registrada(verificacion, "corr-1")

    assert (evento.event_type, evento.event_version, evento.aggregate_id) == (
        "VerificacionConsentimientoRegistrada",
        1,
        CLIENTE,
    )
    assert evento.occurred_at == verificacion.verificado_en
    assert evento.payload == {
        "clienteId": str(CLIENTE),
        "consentimientoId": str(ID),  # el evaluado, aunque la respuesta lo omita
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcancesSolicitados": list(CUATRO),
        "decision": "RECHAZADO",
        "causa": "REVOCADO",
        "alcancesRechazados": list(CUATRO),
        "verificadoEn": "2026-10-18T12:00:00+00:00",
    }


def test_el_evento_de_verificacion_sin_consentimiento_lleva_nulos() -> None:
    verificacion = decidir(VERIFICACION, lectura_verificacion(_fila_verificacion()))
    evento = outbox_pg.verificacion_registrada(verificacion, "corr-1")

    assert evento.payload["consentimientoId"] is None
    assert evento.payload["causa"] == "SIN_CONSENTIMIENTO"
