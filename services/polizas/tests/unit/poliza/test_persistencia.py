"""PolizasPg con conexiones falsas (sin base), HU-W17, 17.6. La base real la prueba
`tests/module/test_poliza_w17.py`."""

from datetime import UTC, datetime
from typing import Any, Self
from uuid import uuid4

import pytest
from polizas.adapters.outbound.polizas_pg import (
    COLUMNAS,
    MUTABLES,
    PolizasPg,
    fila_poliza,
    poliza_de,
)
from polizas.domain.poliza.modelos import (
    Documento,
    EstadoPoliza,
    Firma,
    OrigenPoliza,
    Poliza,
    Producto,
)
from psycopg import sql
from solventa_common.inbox import TransaccionInboxPg

pytestmark = pytest.mark.anyio
CREADA = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
ORIGEN = OrigenPoliza(
    aceptacion_id=uuid4(),
    cotizacion_id=uuid4(),
    cliente_id=uuid4(),
    producto=Producto("vida-hipotecario", "2026.09.1"),
    aceptada_en=datetime(2026, 10, 21, 20, 0, tzinfo=UTC),
)
EMITIENDO = Poliza.emitiendo(ORIGEN, secuencia=7, ahora=CREADA)
DOCUMENTO = Documento(
    clave="polizas/100-2026-000007.pdf",
    version_id="v-1",
    huella="sha256:" + "c" * 64,
    tamano_bytes=4096,
    fecha_documento=datetime(2026, 10, 22, 15, 1, tzinfo=UTC),
)
FIRMA = Firma("FIR-SINT-C01", datetime(2026, 1, 1, tzinfo=UTC))
FIRMADA = EMITIENDO.registrar_documento(DOCUMENTO, CREADA).registrar_firma(
    FIRMA, DOCUMENTO.huella, CREADA
)
ACTIVA = FIRMADA.registrar_fallo("TIEMPO_AGOTADO", CREADA).activar(CREADA)


class Cursor:
    """Devuelve, por cada sentencia, la siguiente fila de `respuestas` (None si se acaban)."""

    def __init__(self, respuestas: list[Any]) -> None:
        self.respuestas = respuestas
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any = None) -> Self:
        self.sentencias.append((sql, parametros))
        return self

    async def fetchone(self) -> Any:
        return self.respuestas.pop(0) if self.respuestas else None

    async def fetchall(self) -> Any:
        return self.respuestas.pop(0) if self.respuestas else []


class Conexion:
    def __init__(self, respuestas: list[Any]) -> None:
        self.cursor_ = Cursor(respuestas)
        self.fabricas: list[Any] = []

    def cursor(self, row_factory: Any = None) -> Cursor:
        self.fabricas.append(row_factory)
        return self.cursor_

    async def execute(self, sql: str, parametros: Any = None) -> Cursor:
        return await self.cursor_.execute(sql, parametros)


def _tx(respuestas: list[Any]) -> tuple[PolizasPg, TransaccionInboxPg, Conexion]:
    conexion = Conexion(respuestas)
    tx = TransaccionInboxPg(conexion, sql.Identifier("inbox"))  # type: ignore[arg-type]
    return PolizasPg(), tx, conexion


@pytest.mark.parametrize(
    "poliza", [EMITIENDO, FIRMADA, ACTIVA], ids=["emitiendo", "firmada", "activa"]
)
def test_la_fila_vuelve_igual(poliza: Poliza) -> None:
    fila = fila_poliza(poliza)
    assert list(fila) == list(COLUMNAS)
    assert poliza_de(fila) == poliza


def test_la_fila_de_una_emitiendo_no_tiene_puntos_de_control() -> None:
    fila = fila_poliza(EMITIENDO)
    assert fila["estado"] == "EMITIENDO"
    assert (fila["ramo_codigo"], fila["ramo_nombre"]) == ("100", "Vida hipotecario")
    assert (fila["vigencia_desde"], fila["vigencia_hasta"]) == (
        EMITIENDO.vigencia.desde,
        EMITIENDO.vigencia.hasta,
    )
    sin_control = [c for c in COLUMNAS if c.startswith(("documento_", "firma_"))]
    assert len(sin_control) == 7
    assert all(fila[c] is None for c in [*sin_control, "emitida_en", "ultimo_fallo"])


def test_las_mutables_no_incluyen_la_identidad() -> None:
    identidad = {"poliza_id", "aceptacion_id", "cotizacion_id", "cliente_id", "numero"}
    assert not identidad & set(MUTABLES)
    assert set(MUTABLES) <= set(COLUMNAS)
    assert "estado" in MUTABLES and "actualizada_en" in MUTABLES


async def test_crear_inserta_en_la_transaccion_recibida() -> None:
    repo, tx, conexion = _tx([(EMITIENDO.poliza_id,)])
    assert await repo.crear(EMITIENDO, tx) is True
    [(insertar, parametros)] = conexion.cursor_.sentencias
    assert insertar.startswith("INSERT INTO polizas (")
    assert insertar.endswith("ON CONFLICT (aceptacion_id) DO NOTHING RETURNING poliza_id")
    assert list(parametros) == [fila_poliza(EMITIENDO)[c] for c in COLUMNAS]


async def test_crear_con_la_aceptacion_ya_emitida_devuelve_false() -> None:
    repo, tx, _ = _tx([None])
    assert await repo.crear(EMITIENDO, tx) is False


async def test_siguiente_secuencia_usa_la_secuencia_de_numeros() -> None:
    repo, tx, conexion = _tx([(41,)])
    assert await repo.siguiente_secuencia(tx) == 41
    [(consulta, _)] = conexion.cursor_.sentencias
    assert consulta == "SELECT nextval('polizas_numero')"


async def test_siguiente_secuencia_sin_fila_falla() -> None:
    repo, tx, _ = _tx([None])
    with pytest.raises(RuntimeError, match="secuencia"):
        await repo.siguiente_secuencia(tx)


async def test_origen_lee_la_aceptacion_y_el_producto_de_su_decision() -> None:
    fila = {
        "aceptacion_id": ORIGEN.aceptacion_id,
        "cotizacion_id": ORIGEN.cotizacion_id,
        "cliente_id": ORIGEN.cliente_id,
        "producto_id": "vida-hipotecario",
        "producto_version": "2026.09.1",
        "aceptada_en": ORIGEN.aceptada_en,
    }
    repo, tx, conexion = _tx([fila])
    assert await repo.origen(ORIGEN.aceptacion_id, tx) == ORIGEN
    [(consulta, parametros)] = conexion.cursor_.sentencias
    assert "JOIN decisiones_suscripcion" in consulta
    assert "insumos ->> 'versionProducto'" in consulta
    assert consulta.endswith("WHERE a.id = %s")
    assert parametros == (ORIGEN.aceptacion_id,)
    assert conexion.fabricas and conexion.fabricas[-1] is not None


async def test_origen_de_una_aceptacion_desconocida_es_none() -> None:
    repo, tx, _ = _tx([None])
    assert await repo.origen(uuid4(), tx) is None


async def test_por_aceptacion() -> None:
    repo, tx, conexion = _tx([fila_poliza(FIRMADA), None])
    assert await repo.por_aceptacion(FIRMADA.aceptacion_id, tx) == FIRMADA
    assert await repo.por_aceptacion(uuid4(), tx) is None
    (consulta, parametros), _ = conexion.cursor_.sentencias
    assert consulta.endswith("FROM polizas WHERE aceptacion_id = %s")
    assert parametros == (FIRMADA.aceptacion_id,)


@pytest.mark.parametrize(("fila", "esperado"), [((ACTIVA.poliza_id,), True), (None, False)])
async def test_actualizar_solo_cambia_una_emitiendo_con_los_mismos_puntos_de_control(
    fila: Any, esperado: bool
) -> None:
    repo, tx, conexion = _tx([fila])
    assert await repo.actualizar(ACTIVA, tx) is esperado
    [(actualizar, parametros)] = conexion.cursor_.sentencias
    assert actualizar.startswith("UPDATE polizas SET ")
    # HU-W17 (17.8): además de EMITIENDO, cada punto de control ya registrado debe ser el de la
    # copia que se escribe; si otra tarea registró otro (o lo registró y esta copia no lo tiene),
    # no cambia nada en lugar de chocar con el disparador de 004.
    assert actualizar.endswith(
        "WHERE poliza_id = %s AND estado = 'EMITIENDO' "
        "AND (documento_version_id IS NULL OR documento_version_id = %s) "
        "AND (firma_id IS NULL OR (firma_id, firma_sello_tiempo) = (%s, %s)) "
        "RETURNING poliza_id"
    )
    asignaciones = actualizar.removeprefix("UPDATE polizas SET ").split(" WHERE ")[0]
    # 17.8 (ajuste): al quedar ACTIVA, la reserva se libera en la misma escritura.
    assert asignaciones == ", ".join(f"{c} = %s" for c in MUTABLES) + (
        ", reservada_hasta = CASE WHEN %s::text = 'ACTIVA' THEN NULL ELSE reservada_hasta END"
    )
    fila_activa = fila_poliza(ACTIVA)
    assert list(parametros) == [
        *(fila_activa[c] for c in MUTABLES),
        "ACTIVA",
        ACTIVA.poliza_id,
        DOCUMENTO.version_id,
        FIRMA.firma_id,
        FIRMA.sello_tiempo,
    ]
    assert fila_activa["estado"] == EstadoPoliza.ACTIVA.value


async def test_la_guarda_de_una_copia_sin_puntos_de_control_exige_que_sigan_vacios() -> None:
    repo, tx, conexion = _tx([None])
    assert (
        await repo.actualizar(EMITIENDO.registrar_fallo("FIRMA_NO_DISPONIBLE", CREADA), tx) is False
    )
    [(_, parametros)] = conexion.cursor_.sentencias
    # documento_version_id = NULL y (firma_id, sello) = (NULL, NULL) no son verdaderos: solo pasa
    # si siguen vacíos.
    assert list(parametros)[-3:] == [None, None, None]


def test_la_correlacion_es_de_la_identidad_y_no_cambia() -> None:
    con_correlacion = Poliza.emitiendo(ORIGEN, secuencia=8, ahora=CREADA, correlation_id="corr-1")
    fila = fila_poliza(con_correlacion)
    assert fila["correlation_id"] == "corr-1"
    assert poliza_de(fila) == con_correlacion
    assert "correlation_id" in COLUMNAS and "correlation_id" not in MUTABLES
    # Una fila anterior a 005 (sin la columna) se lee sin correlación.
    sin_columna = {c: v for c, v in fila_poliza(EMITIENDO).items() if c != "correlation_id"}
    assert poliza_de(sin_columna).correlation_id is None


async def test_por_id() -> None:
    repo, tx, conexion = _tx([fila_poliza(FIRMADA), None])
    assert await repo.por_id(FIRMADA.poliza_id, tx) == FIRMADA
    assert await repo.por_id(uuid4(), tx) is None
    (consulta, parametros), _ = conexion.cursor_.sentencias
    assert consulta.endswith("FROM polizas WHERE poliza_id = %s")
    assert parametros == (FIRMADA.poliza_id,)


async def test_pendientes_sin_reserva_vigente_nuevas_o_con_fallo_de_hace_reintento_s() -> None:
    ahora = datetime(2026, 10, 22, 15, 1, tzinfo=UTC)
    antes_de = datetime(2026, 10, 22, 15, 0, 30, tzinfo=UTC)
    repo, tx, conexion = _tx([[fila_poliza(EMITIENDO), fila_poliza(FIRMADA)]])
    assert await repo.pendientes(ahora, antes_de, 7, tx) == [EMITIENDO, FIRMADA]
    [(consulta, parametros)] = conexion.cursor_.sentencias
    assert consulta.startswith(f"SELECT {', '.join(COLUMNAS)} FROM polizas ")
    # 17.8 (ajuste): la reserva protege a las que están en curso; sin fallo es «nunca intentada»
    # (o una tarea que cayó y cuya reserva venció).
    assert consulta.endswith(
        "WHERE estado = 'EMITIENDO' "
        "AND (reservada_hasta IS NULL OR reservada_hasta < %s) "
        "AND (ultimo_fallo IS NULL OR actualizada_en < %s) "
        "ORDER BY ultimo_fallo IS NOT NULL, actualizada_en, poliza_id LIMIT %s"
    )
    assert parametros == (ahora, antes_de, 7)
    # Sin FOR UPDATE: no se bloquean filas durante las llamadas.
    assert "FOR UPDATE" not in consulta


async def test_pendientes_sin_filas() -> None:
    repo, tx, _ = _tx([[]])
    assert await repo.pendientes(CREADA, CREADA, 10, tx) == []


@pytest.mark.parametrize(("fila", "tomada"), [(True, True), (False, False)])
async def test_reservar_solo_si_sigue_emitiendo_y_sin_reserva_vigente(
    fila: bool, tomada: bool
) -> None:
    ahora = datetime(2026, 10, 22, 15, 1, tzinfo=UTC)
    hasta = datetime(2026, 10, 22, 15, 2, tzinfo=UTC)
    repo, tx, conexion = _tx([fila_poliza(FIRMADA) if fila else None])
    resultado = await repo.reservar(FIRMADA.poliza_id, ahora, hasta, tx)
    assert resultado == (FIRMADA if tomada else None)
    [(sentencia, parametros)] = conexion.cursor_.sentencias
    assert sentencia == (
        "UPDATE polizas SET reservada_hasta = %s "
        "WHERE poliza_id = %s AND estado = 'EMITIENDO' "
        "AND (reservada_hasta IS NULL OR reservada_hasta < %s) "
        f"RETURNING {', '.join(COLUMNAS)}"
    )
    assert parametros == (hasta, FIRMADA.poliza_id, ahora)


async def test_liberar_solo_la_reserva_propia() -> None:
    hasta = datetime(2026, 10, 22, 15, 2, tzinfo=UTC)
    repo, tx, conexion = _tx([])
    await repo.liberar(FIRMADA.poliza_id, hasta, tx)
    [(sentencia, parametros)] = conexion.cursor_.sentencias
    # Con la hora de la propia: una reserva vencida que tomó otra tarea no se libera.
    assert sentencia == (
        "UPDATE polizas SET reservada_hasta = NULL "
        "WHERE poliza_id = %s AND estado = 'EMITIENDO' AND reservada_hasta = %s"
    )
    assert parametros == (FIRMADA.poliza_id, hasta)


async def test_las_transacciones_abren_una_conexion_del_pool_con_transaccion() -> None:
    from contextlib import asynccontextmanager

    from polizas.adapters.outbound.polizas_pg import TransaccionesPg

    eventos: list[str] = []

    class ConexionPool:
        @asynccontextmanager
        async def transaction(self) -> Any:
            eventos.append("begin")
            yield
            eventos.append("commit")

    class Pool:
        @asynccontextmanager
        async def connection(self) -> Any:
            eventos.append("conexion")
            yield conexion

    conexion = ConexionPool()
    async with TransaccionesPg(Pool()).transaccion() as tx:  # type: ignore[arg-type]
        assert tx.conexion is conexion
        eventos.append("trabajo")
    assert eventos == ["conexion", "begin", "trabajo", "commit"]


# --- La lectura (HU-W17, 17.9) ----------------------------------------------------------------
class PoolLectura:
    """Un pool que entrega siempre la misma conexión falsa y cuenta las que abre."""

    def __init__(self, respuestas: list[Any]) -> None:
        self.conexion = Conexion(respuestas)
        self.abiertas = 0

    def connection(self) -> Any:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def abrir() -> Any:
            self.abiertas += 1
            yield self.conexion

        return abrir()


@pytest.mark.parametrize(
    ("metodo", "valor", "filtro"),
    [
        ("por_aceptacion", ACTIVA.aceptacion_id, "aceptacion_id"),
        ("por_id", ACTIVA.poliza_id, "poliza_id"),
        ("por_numero", ACTIVA.numero, "numero"),
    ],
)
async def test_la_lectura_usa_el_pool_una_conexion_por_consulta(
    metodo: str, valor: Any, filtro: str
) -> None:
    from polizas.adapters.outbound.polizas_pg import PolizasLecturaPg

    pool = PoolLectura([fila_poliza(ACTIVA), None])
    repo = PolizasLecturaPg(pool)  # type: ignore[arg-type]
    assert await getattr(repo, metodo)(valor) == ACTIVA
    assert await getattr(repo, metodo)(valor) is None
    assert pool.abiertas == 2
    (consulta, parametros), _ = pool.conexion.cursor_.sentencias
    assert consulta.endswith(f"FROM polizas WHERE {filtro} = %s")
    assert parametros == (valor,)
    # Todas las columnas de la póliza, sin la reserva.
    assert consulta.startswith(f"SELECT {', '.join(COLUMNAS)} FROM")
