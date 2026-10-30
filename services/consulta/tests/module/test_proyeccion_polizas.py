"""MT-M10-F1: la proyección de pólizas de `consulta` con PostgreSQL real.

Un `PolizaEmitida` sintético (conforme al esquema publicado) entra por el consumidor real
(`ConsumidorInbox`, `InboxPg`, el manejador y el caso de uso); el catálogo es el adaptador HTTP real
sobre un transporte simulado. Cubre la proyección, el duplicado, la versión distinta, el catálogo
caído, los tipos ignorados y la migración hacia adelante, hacia atrás y otra vez hacia adelante.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from consulta.adapters.outbound.catalogo_http import CatalogoHttp
from consulta.adapters.outbound.polizas_pg import migrar
from consulta.main import armar_consumidor
from jsonschema import Draft202012Validator
from psycopg_pool import AsyncConnectionPool
from solventa_common.inbox import MensajeSqs

pytestmark = pytest.mark.anyio
EVENTOS = Path(__file__).parents[4] / "contracts/events"
MIGRACIONES = resources.files("consulta.adapters.outbound").joinpath("migraciones")
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _producto(version: str = "1.0.0") -> dict[str, Any]:
    return {
        "id": "vida-deudor",
        "version": version,
        "nombre": "Vida deudor",
        "ramo": "VIDA",
        "pais": "CO",
        "moneda": "COP",
        "estado": "ACTIVO",
        "vigenteDesde": "2026-01-01T00:00:00Z",
        "vigenteHasta": None,
        "coberturas": [
            {"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True},
            {"codigo": "INCAPACIDAD", "nombre": "Incapacidad", "obligatoria": False},
        ],
    }


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


class ReceptorFalso:
    def __init__(self, *cuerpos: dict[str, Any]) -> None:
        self.pendientes = [
            MensajeSqs(f"m-{i}", f"r-{i}", json.dumps(c), c["type"], 1)
            for i, c in enumerate(cuerpos)
        ]
        self.borrados: list[str] = []

    async def recibir(self) -> list[MensajeSqs]:
        lote, self.pendientes = self.pendientes, []
        return lote

    async def borrar(self, mensaje: MensajeSqs) -> None:
        self.borrados.append(mensaje.message_id)


def _filas(url: str, sql: str, parametros: Any = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.execute(sql, parametros).fetchall()


def _ejecutar(url: str, sql: str) -> None:
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(sql)


@pytest.fixture(autouse=True)
def limpia(base: str) -> None:
    _ejecutar(base, "TRUNCATE poliza_cliente, inbox")


@asynccontextmanager
async def _consumir(
    base: str, receptor: ReceptorFalso, catalogo: httpx.Response | Exception
) -> AsyncIterator[int]:
    def catalogo_simulado(_: httpx.Request) -> httpx.Response:
        if isinstance(catalogo, Exception):
            raise catalogo
        return catalogo

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(catalogo_simulado))
    async with AsyncConnectionPool(base, min_size=1, max_size=2, open=False) as pool:
        consumidor = armar_consumidor(
            pool, receptor, CatalogoHttp(cliente, "http://catalogo", 300), RelojFijo()
        )
        yield await consumidor.ejecutar_una_vez()
    await cliente.aclose()


def _vigente(version: str = "1.0.0") -> httpx.Response:
    return httpx.Response(200, content=json.dumps(_producto(version)))


def test_el_evento_sintetico_cumple_el_esquema_publicado(sobre_poliza: Any) -> None:
    esquema = json.loads((EVENTOS / "PolizaEmitida.v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(esquema).iter_errors(sobre_poliza())) == []


async def test_proyecta_la_poliza_con_el_ramo_y_las_coberturas_del_catalogo(
    base: str, sobre_poliza: Any
) -> None:
    sobre = sobre_poliza()
    receptor = ReceptorFalso(sobre)

    async with _consumir(base, receptor, _vigente()) as procesados:
        assert procesados == 1

    (fila,) = _filas(
        base,
        "SELECT poliza_id, cliente_id, numero, estado, producto_nombre, ramo, vigencia_desde, "
        "vigencia_hasta, prima, suma_asegurada, coberturas, emitida_en, proyectada_en "
        "FROM poliza_cliente",
    )
    payload = sobre["payload"]
    assert fila[0] == UUID(payload["polizaId"])
    assert fila[1] == UUID(payload["clienteId"])
    assert fila[2:6] == ("100-2026-000001", "ACTIVA", "Vida deudor", "VIDA")
    assert fila[6:8] == (date(2026, 10, 25), date(2027, 10, 24))
    assert fila[8:10] == (Decimal("64638.10"), Decimal(50000000))
    assert [c["codigo"] for c in fila[10]] == ["MUERTE", "INCAPACIDAD"]
    assert fila[11] == datetime(2026, 10, 24, 15, 0, 1, 250000, tzinfo=UTC)
    assert fila[12] == AHORA
    assert _filas(base, "SELECT event_type FROM inbox") == [("PolizaEmitida",)]
    assert receptor.borrados == ["m-0"]


async def test_un_duplicado_por_event_id_o_por_poliza_id_no_modifica_la_fila(
    base: str, sobre_poliza: Any
) -> None:
    original = sobre_poliza()
    mismo_event_id = original | {}
    otro_event_id = sobre_poliza(polizaId=original["payload"]["polizaId"], prima="1.00")
    receptor = ReceptorFalso(original, mismo_event_id, otro_event_id)

    async with _consumir(base, receptor, _vigente()) as procesados:
        assert procesados == 3

    assert _filas(base, "SELECT prima FROM poliza_cliente") == [(Decimal("64638.10"),)]
    assert _filas(base, "SELECT count(*) FROM inbox") == [(2,)]
    assert receptor.borrados == ["m-0", "m-1", "m-2"]


async def test_version_distinta_a_la_vigente_no_proyecta_ni_borra_ni_registra_el_inbox(
    base: str, sobre_poliza: Any
) -> None:
    receptor = ReceptorFalso(sobre_poliza())

    async with _consumir(base, receptor, _vigente("2.0.0")) as procesados:
        assert procesados == 0

    assert _filas(base, "SELECT count(*) FROM poliza_cliente") == [(0,)]
    assert _filas(base, "SELECT count(*) FROM inbox") == [(0,)]
    assert receptor.borrados == []


@pytest.mark.parametrize(
    "catalogo",
    [
        httpx.ConnectError("sin conexión"),
        httpx.Response(503),
        httpx.Response(404, json={"codigo": "PRODUCTO_NO_ENCONTRADO"}),
    ],
)
async def test_catalogo_caido_no_proyecta_y_el_mensaje_se_reintenta(
    base: str, sobre_poliza: Any, catalogo: httpx.Response | Exception
) -> None:
    receptor = ReceptorFalso(sobre_poliza())

    async with _consumir(base, receptor, catalogo) as procesados:
        assert procesados == 0

    assert _filas(base, "SELECT count(*) FROM poliza_cliente") == [(0,)]
    assert _filas(base, "SELECT count(*) FROM inbox") == [(0,)]
    assert receptor.borrados == []


async def test_los_otros_tipos_se_ignoran_y_poliza_emitida_v2_no_se_borra(
    base: str, sobre_poliza: Any
) -> None:
    ajeno = sobre_poliza(sobre={"type": "SiniestroReportado"})
    v2 = sobre_poliza(sobre={"version": 2})
    receptor = ReceptorFalso(ajeno, v2)

    async with _consumir(base, receptor, _vigente()) as procesados:
        assert procesados == 1

    assert _filas(base, "SELECT count(*) FROM poliza_cliente") == [(0,)]
    assert _filas(base, "SELECT event_type FROM inbox") == [("SiniestroReportado",)]
    assert receptor.borrados == ["m-0"]


def test_la_migracion_va_hacia_adelante_hacia_atras_y_otra_vez_hacia_adelante(base: str) -> None:
    abajo = MIGRACIONES.joinpath("down/001_poliza_cliente.sql").read_text(encoding="utf-8")
    tablas = "SELECT tablename FROM pg_tables WHERE tablename IN ('poliza_cliente', 'inbox')"

    migrar(base)  # idempotente: se aplica en cada arranque
    assert sorted(_filas(base, tablas)) == [("inbox",), ("poliza_cliente",)]
    _ejecutar(base, abajo)
    assert _filas(base, tablas) == []
    migrar(base)
    assert sorted(_filas(base, tablas)) == [("inbox",), ("poliza_cliente",)]
    assert _filas(
        base, "SELECT indexname FROM pg_indexes WHERE indexname = 'poliza_cliente_cliente_idx'"
    ) == [("poliza_cliente_cliente_idx",)]
