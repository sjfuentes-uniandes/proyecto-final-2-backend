"""MT-M10-F2: la API interna de lectura de pólizas de `consulta` con PostgreSQL real.

Cubre la póliza propia, la ajena (403 sin datos), la inexistente (404), el actor ausente o con otro
rol, la lista vacía y la lista con vencidas ordenada por `vigencia_hasta` descendente.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from consulta.adapters.outbound.polizas_pg import COLUMNAS, fila_de
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada, Vigencia
from consulta.main import crear_app
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
ACTOR = {"X-Actor-Id": "sub-1", "X-Actor-Rol": "CLIENTE"}


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


class CatalogoNulo:
    async def producto(self, producto_id: str) -> Any:
        raise AssertionError("la lectura no consulta el catálogo")


def _poliza(cliente_id: UUID, desde: date, hasta: date, numero: str) -> PolizaProyectada:
    return PolizaProyectada(
        poliza_id=uuid4(),
        cliente_id=cliente_id,
        numero=numero,
        estado="ACTIVA",
        producto_id="vida-deudor",
        producto_version="1.0.0",
        producto_nombre="Vida deudor",
        ramo="VIDA",
        vigencia=Vigencia(desde, hasta),
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


def _guardar(url: str, *polizas: PolizaProyectada) -> None:
    sql = (
        f"INSERT INTO poliza_cliente ({', '.join(COLUMNAS)}) "
        f"VALUES ({', '.join(['%s'] * len(COLUMNAS))})"
    )
    with psycopg.connect(url, autocommit=True) as conexion:
        for p in polizas:
            conexion.execute(sql, fila_de(p))


@pytest.fixture(autouse=True)
def limpia(base: str) -> None:
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE poliza_cliente, inbox")


@pytest.fixture
async def api(base: str) -> AsyncIterator[httpx.AsyncClient]:
    async with AsyncConnectionPool(base, min_size=1, max_size=2, open=False) as pool:
        app = crear_app(catalogo=CatalogoNulo(), reloj=RelojFijo(), pool=pool)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://consulta"
            ) as cliente,
        ):
            yield cliente


async def test_el_detalle_de_la_propia_devuelve_vigencia_coberturas_y_credencial(
    base: str, api: httpx.AsyncClient
) -> None:
    cliente = uuid4()
    p = _poliza(cliente, date(2026, 10, 25), date(2027, 10, 24), "100-2026-000001")
    _guardar(base, p)

    r = await api.get(f"/v1/clientes/{cliente}/polizas/{p.poliza_id}", headers=ACTOR)

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["polizaId"] == str(p.poliza_id)
    assert cuerpo["numero"] == "100-2026-000001"
    assert cuerpo["productoNombre"] == p.producto_nombre
    assert cuerpo["ramo"] == p.ramo
    assert cuerpo["vigencia"] == {"desde": "2026-10-25", "hasta": "2027-10-24"}
    assert cuerpo["vigente"] is True
    assert cuerpo["coberturas"] == [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}]
    assert cuerpo["documentoHuella"] == p.documento_huella
    assert cuerpo["firmaId"] == "FIRMA-0001"
    assert Decimal(cuerpo["prima"]) == Decimal("64638.10")


async def test_el_detalle_de_una_ajena_es_403_sin_datos_de_la_poliza(
    base: str, api: httpx.AsyncClient
) -> None:
    ajena = _poliza(uuid4(), date(2026, 10, 25), date(2027, 10, 24), "100-2026-000002")
    _guardar(base, ajena)

    r = await api.get(f"/v1/clientes/{uuid4()}/polizas/{ajena.poliza_id}", headers=ACTOR)

    assert r.status_code == 403
    assert r.json()["codigo"] == "POLIZA_NO_AUTORIZADA"
    for secreto in ("100-2026-000002", ajena.documento_huella, str(ajena.cliente_id), "MUERTE"):
        assert secreto not in r.text


async def test_el_detalle_de_una_inexistente_es_404(api: httpx.AsyncClient) -> None:
    r = await api.get(f"/v1/clientes/{uuid4()}/polizas/{uuid4()}", headers=ACTOR)

    assert r.status_code == 404
    assert r.json()["codigo"] == "POLIZA_NO_ENCONTRADA"


@pytest.mark.parametrize(
    ("encabezados", "estado", "codigo"),
    [
        ({}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": "sub-1"}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": "sub-1", "X-Actor-Rol": "ASESOR"}, 403, "ROL_NO_AUTORIZADO"),
    ],
)
async def test_sin_actor_valido_se_rechaza_en_lista_y_detalle(
    api: httpx.AsyncClient, encabezados: dict[str, str], estado: int, codigo: str
) -> None:
    cliente = uuid4()
    for ruta in (f"/v1/clientes/{cliente}/polizas", f"/v1/clientes/{cliente}/polizas/{uuid4()}"):
        r = await api.get(ruta, headers=encabezados)
        assert r.status_code == estado
        assert r.json()["codigo"] == codigo


async def test_la_lista_incluye_vencidas_ordenada_y_solo_las_del_cliente(
    base: str, api: httpx.AsyncClient
) -> None:
    cliente = uuid4()
    vencida = _poliza(cliente, date(2025, 1, 1), date(2025, 12, 31), "100-2026-000010")
    vigente = _poliza(cliente, date(2026, 10, 25), date(2027, 10, 24), "100-2026-000011")
    otra_vigente = _poliza(cliente, date(2026, 1, 1), date(2026, 12, 31), "100-2026-000012")
    ajena = _poliza(uuid4(), date(2026, 1, 1), date(2030, 1, 1), "100-2026-000013")
    _guardar(base, vencida, vigente, otra_vigente, ajena)

    r = await api.get(f"/v1/clientes/{cliente}/polizas", headers=ACTOR)

    assert r.status_code == 200
    assert [(p["numero"], p["vigente"]) for p in r.json()] == [
        ("100-2026-000011", True),
        ("100-2026-000012", True),
        ("100-2026-000010", False),
    ]


async def test_sin_polizas_la_lista_es_vacia_con_200(api: httpx.AsyncClient) -> None:
    r = await api.get(f"/v1/clientes/{uuid4()}/polizas", headers=ACTOR)

    assert r.status_code == 200
    assert r.json() == []
