"""MT-W18-F1: estado y listado de pólizas de `consulta` con PostgreSQL real.

Cubre el estado en sus cuatro valores, el 404 uniforme, el filtro de `estado` en SQL contra
`estado_de`, los demás filtros, la paginación, el orden, `actualizadoEn` y los índices.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from uuid import uuid4

import httpx
import psycopg
import pytest
from consulta.adapters.outbound.polizas_pg import COLUMNAS, fila_de
from consulta.domain.polizas.estado import EstadoVigencia, estado_de
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada, Vigencia
from consulta.main import crear_app
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
HOY = date(2026, 10, 25)
ASESOR = {"X-Actor-Id": "sub-1", "X-Actor-Rol": "ASESOR"}
N = 30


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


class CatalogoNulo:
    async def producto(self, producto_id: str) -> object:
        raise AssertionError("la lectura no consulta el catálogo")


def _poliza(
    numero: str, desde: date, hasta: date, *, ramo: str = "VIDA", minutos: int = 0
) -> PolizaProyectada:
    return PolizaProyectada(
        poliza_id=uuid4(),
        cliente_id=uuid4(),
        numero=numero,
        estado="ACTIVA",
        producto_id="vida-deudor",
        producto_version="1.0.0",
        producto_nombre="Vida deudor",
        ramo=ramo,
        vigencia=Vigencia(desde, hasta),
        prima=Decimal("64638.10"),
        suma_asegurada=Decimal(50000000),
        moneda="COP",
        periodicidad="MENSUAL",
        coberturas=(CoberturaProyectada("MUERTE", "Muerte", True),),
        documento_huella="sha256:" + "a" * 64,
        firma_id="FIRMA-0001",
        emitida_en=AHORA - timedelta(minutes=minutos),
        proyectada_en=AHORA - timedelta(minutes=minutos) + timedelta(seconds=5),
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
        app = crear_app(catalogo=CatalogoNulo(), reloj=RelojFijo(), pool=pool)  # type: ignore[arg-type]
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://consulta"
            ) as cliente,
        ):
            yield cliente


@pytest.mark.parametrize(
    ("desde", "hasta", "estado"),
    [
        (date(2026, 1, 1), date(2027, 6, 30), "VIGENTE"),
        (date(2025, 11, 5), date(2026, 11, 4), "POR_VENCER"),
        (date(2025, 1, 1), date(2025, 12, 31), "VENCIDA"),
        (date(2026, 11, 1), date(2027, 10, 31), "NO_INICIADA"),
    ],
)
async def test_detalle_por_estado(
    base: str, api: httpx.AsyncClient, desde: date, hasta: date, estado: str
) -> None:
    p = _poliza("100-2026-000001", desde, hasta)
    _guardar(base, p)

    r = await api.get(f"/v1/polizas/{p.poliza_id}/estado", headers=ASESOR)

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["estado"] == estado
    assert datetime.fromisoformat(cuerpo["fechaActualizacion"]) == p.proyectada_en
    assert datetime.fromisoformat(cuerpo["emitidaEn"]) == p.emitida_en
    assert cuerpo["coberturas"] == [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}]
    assert Decimal(cuerpo["prima"]) == Decimal("64638.10")
    for campo in ("clienteId", "documentoHuella", "firmaId"):
        assert campo not in r.text


async def test_inexistente_y_no_proyectada_responden_el_mismo_404(
    base: str, api: httpx.AsyncClient
) -> None:
    existente = _poliza("100-2026-000001", date(2026, 1, 1), date(2027, 1, 1))
    _guardar(base, existente)
    aun_no_proyectada = uuid4()

    a = await api.get(f"/v1/polizas/{uuid4()}/estado", headers=ASESOR)
    b = await api.get(f"/v1/polizas/{aun_no_proyectada}/estado", headers=ASESOR)
    _guardar(base, _poliza("100-2026-000002", date(2026, 1, 1), date(2027, 1, 1)))
    c = await api.get(f"/v1/polizas/{aun_no_proyectada}/estado", headers=ASESOR)

    assert (a.status_code, b.status_code, c.status_code) == (404, 404, 404)
    cuerpos = [{k: v for k, v in r.json().items() if k != "correlationId"} for r in (a, b, c)]
    assert cuerpos[0] == cuerpos[1] == cuerpos[2]
    assert a.json()["correlationId"] != b.json()["correlationId"]
    assert a.json()["codigo"] == "POLIZA_NO_ENCONTRADA"


async def test_otro_rol_es_403_sin_datos(base: str, api: httpx.AsyncClient) -> None:
    p = _poliza("100-2026-000001", date(2026, 1, 1), date(2027, 1, 1))
    _guardar(base, p)

    for ruta in ("/v1/polizas", f"/v1/polizas/{p.poliza_id}/estado"):
        r = await api.get(ruta, headers={"X-Actor-Id": "a", "X-Actor-Rol": "CLIENTE"})
        assert (r.status_code, r.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")
        assert p.numero not in r.text


def _sembrar_bordes(base: str) -> list[PolizaProyectada]:
    """Una póliza por cada borde de los estados, con ramos y emisiones distintos."""
    bordes = [
        (date(2026, 10, 26), date(2027, 10, 25)),  # empieza mañana
        (date(2026, 10, 25), date(2027, 10, 24)),  # empieza hoy
        (date(2026, 1, 1), date(2026, 11, 25)),  # faltan 31 días
        (date(2026, 1, 1), date(2026, 11, 24)),  # faltan 30 días
        (date(2026, 1, 1), date(2026, 10, 25)),  # termina hoy
        (date(2025, 1, 1), date(2026, 10, 24)),  # terminó ayer
    ]
    polizas = [
        _poliza(f"100-2026-{i:06d}", d, h, ramo="VIDA" if i % 2 else "AUTOS", minutos=i)
        for i, (d, h) in enumerate(bordes, start=1)
    ]
    _guardar(base, *polizas)
    return polizas


@pytest.mark.parametrize("estado", list(EstadoVigencia))
async def test_el_filtro_de_estado_coincide_con_estado_de(
    base: str, api: httpx.AsyncClient, estado: EstadoVigencia
) -> None:
    polizas = _sembrar_bordes(base)

    r = await api.get(f"/v1/polizas?estado={estado}", headers=ASESOR)

    esperadas = {p.numero for p in polizas if estado_de(p.vigencia, HOY, N) is estado}
    assert esperadas
    assert {i["numero"] for i in r.json()["items"]} == esperadas
    assert r.json()["total"] == len(esperadas)
    assert {i["estado"] for i in r.json()["items"]} == {estado}


async def test_orden_por_emision_descendente_y_resumen(base: str, api: httpx.AsyncClient) -> None:
    _sembrar_bordes(base)

    r = await api.get("/v1/polizas", headers=ASESOR)

    cuerpo = r.json()
    assert [i["numero"] for i in cuerpo["items"]] == [f"100-2026-{i:06d}" for i in range(1, 7)]
    assert cuerpo["total"] == 6
    assert (cuerpo["pagina"], cuerpo["tamano"]) == (1, 20)
    assert set(cuerpo["items"][0]) == {
        "polizaId", "numero", "ramo", "producto", "prima", "moneda", "vigenciaHasta", "estado",
        "fechaActualizacion",
    }  # fmt: skip
    for campo in ("clienteId", "documentoHuella", "firmaId"):
        assert campo not in r.text


async def test_desempate_por_poliza_id(base: str, api: httpx.AsyncClient) -> None:
    mismas = [
        _poliza(f"100-2026-00000{i}", date(2026, 1, 1), date(2027, 1, 1), minutos=0)
        for i in range(1, 4)
    ]
    _guardar(base, *mismas)

    r = await api.get("/v1/polizas", headers=ASESOR)

    ids = [i["polizaId"] for i in r.json()["items"]]
    assert ids == sorted(str(p.poliza_id) for p in mismas)


@pytest.mark.parametrize(
    ("consulta", "esperadas"),
    [
        ("numero=100-2026-000003", [3]),
        ("ramo=AUTOS", [2, 4, 6]),
        ("ramo=VID", []),  # igualdad exacta, no prefijo
        ("vigenciaDesde=2026-10-25", [1, 2, 3, 4, 5]),  # termina ese día o después
        ("vigenciaDesde=2026-10-26", [1, 2, 3, 4]),
        ("estado=POR_VENCER&ramo=AUTOS", [4]),
    ],
)
async def test_filtros(
    base: str, api: httpx.AsyncClient, consulta: str, esperadas: list[int]
) -> None:
    _sembrar_bordes(base)

    r = await api.get(f"/v1/polizas?{consulta}", headers=ASESOR)

    assert [int(i["numero"][-6:]) for i in r.json()["items"]] == esperadas
    assert r.json()["total"] == len(esperadas)


async def test_paginacion_total_y_actualizado_en(base: str, api: httpx.AsyncClient) -> None:
    polizas = _sembrar_bordes(base)
    ultima = max(p.proyectada_en for p in polizas)

    p1 = (await api.get("/v1/polizas?pagina=1&tamano=4", headers=ASESOR)).json()
    p2 = (await api.get("/v1/polizas?pagina=2&tamano=4", headers=ASESOR)).json()
    p3 = (await api.get("/v1/polizas?pagina=3&tamano=4", headers=ASESOR)).json()
    filtrada = (await api.get("/v1/polizas?ramo=AUTOS", headers=ASESOR)).json()

    assert [len(p["items"]) for p in (p1, p2, p3)] == [4, 2, 0]
    assert [p["total"] for p in (p1, p2, p3)] == [6, 6, 6]
    assert [i["numero"] for i in p1["items"] + p2["items"]] == [
        f"100-2026-{i:06d}" for i in range(1, 7)
    ]
    # El mayor proyectada_en de toda la proyección, sin importar el filtro ni la página.
    for pagina in (p1, p2, p3, filtrada):
        assert datetime.fromisoformat(pagina["actualizadoEn"]) == ultima


async def test_actualizado_en_avanza_con_una_nueva_proyeccion(
    base: str, api: httpx.AsyncClient
) -> None:
    _guardar(base, _poliza("100-2026-000001", date(2026, 1, 1), date(2027, 1, 1), minutos=10))
    antes = (await api.get("/v1/polizas", headers=ASESOR)).json()["actualizadoEn"]
    _guardar(base, _poliza("100-2026-000002", date(2026, 1, 1), date(2027, 1, 1), minutos=0))

    despues = (await api.get("/v1/polizas", headers=ASESOR)).json()["actualizadoEn"]

    assert datetime.fromisoformat(despues) > datetime.fromisoformat(antes)


async def test_lista_vacia(api: httpx.AsyncClient) -> None:
    r = await api.get("/v1/polizas", headers=ASESOR)

    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0, "pagina": 1, "tamano": 20, "actualizadoEn": None}


def test_la_migracion_de_indices_es_idempotente_y_reversible(base: str) -> None:
    from consulta.adapters.outbound.polizas_pg import migrar

    migrar(base)
    consulta = "SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'poliza_cliente_%_idx'"
    reversa = (
        files("consulta.adapters.outbound")
        .joinpath("migraciones/down/002_indices_estado_polizas.sql")
        .read_text(encoding="utf-8")
    )
    with psycopg.connect(base, autocommit=True) as conexion:
        fila = conexion.execute(consulta).fetchone()
        assert fila is not None and fila[0] == 5  # cliente + los cuatro de HU-W18
        conexion.execute(reversa)  # type: ignore[arg-type]
        fila = conexion.execute(consulta).fetchone()
        assert fila is not None and fila[0] == 1
    migrar(base)
