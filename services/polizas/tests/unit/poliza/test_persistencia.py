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
async def test_actualizar_solo_cambia_una_emitiendo(fila: Any, esperado: bool) -> None:
    repo, tx, conexion = _tx([fila])
    assert await repo.actualizar(ACTIVA, tx) is esperado
    [(actualizar, parametros)] = conexion.cursor_.sentencias
    assert actualizar.startswith("UPDATE polizas SET ")
    assert actualizar.endswith("WHERE poliza_id = %s AND estado = 'EMITIENDO' RETURNING poliza_id")
    asignaciones = actualizar.removeprefix("UPDATE polizas SET ").split(" WHERE ")[0]
    assert asignaciones == ", ".join(f"{c} = %s" for c in MUTABLES)
    fila_activa = fila_poliza(ACTIVA)
    assert list(parametros) == [*(fila_activa[c] for c in MUTABLES), ACTIVA.poliza_id]
    assert fila_activa["estado"] == EstadoPoliza.ACTIVA.value
