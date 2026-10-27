"""Listado de cotizaciones con oferta ("Cotizaciones abiertas" del portal): estado derivado, cursor,
alcance del socio, SQL del repositorio y la ruta GET /v1/cotizaciones con fakes."""

import base64
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from cotizacion.adapters.outbound.cotizaciones_pg import (
    CotizacionesPg,
    filtros_listado,
    resumen_de,
)
from cotizacion.application.cotizacion.listar import (
    ListarCotizaciones,
    codificar_cursor,
    decodificar_cursor,
)
from cotizacion.config import Settings
from cotizacion.domain.cotizacion.errores import CursorInvalido
from cotizacion.domain.cotizacion.listado import FiltrosCotizaciones, ResumenCotizacion
from cotizacion.domain.cotizacion.modelos import Actor, EstadoOferta, RolActor
from cotizacion.domain.perfilamiento.modelos import NivelRiesgo
from cotizacion.main import Puertos, crear_app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.anyio

# El mismo instante que el reloj de escenario_cotizacion (conftest).
AHORA = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
ASESOR = Actor("asesor-sintetico", RolActor.ASESOR)
SOCIO = Actor("SOC-000001", RolActor.SOCIO)
CABECERAS_ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
CABECERAS_SOCIO = {"X-Actor-Id": "SOC-000001", "X-Actor-Rol": "SOCIO"}


def resumen(n: int, horas_vigencia: int = 168, **cambios: Any) -> ResumenCotizacion:
    creada = AHORA - timedelta(hours=n)
    datos: dict[str, Any] = {
        "cotizacion_id": UUID(f"00000000-0000-4000-d000-{n:012d}"),
        "codigo": f"COT-2026-{n:05d}",
        "cliente_id": UUID("00000000-0000-4000-8000-000000000001"),
        "socio_id": None,
        "producto_id": "vida-hipotecario",
        "version_producto": "2026.09.1",
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "suma_asegurada": Decimal(180000000),
        "prima": Decimal(33264),
        "nivel_riesgo": NivelRiesgo.BAJO,
        "degradada": False,
        "creada_en": creada,
        "vigente_hasta": creada + timedelta(hours=horas_vigencia),
    }
    return ResumenCotizacion(**(datos | cambios))


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


class ListadoFalso:
    """Devuelve las filas en el orden dado y registra lo que pidió el caso de uso."""

    def __init__(self, filas: list[ResumenCotizacion]) -> None:
        self.filas = filas
        self.pedidos: list[tuple[FiltrosCotizaciones, int, Any]] = []

    async def listar(
        self, filtros: FiltrosCotizaciones, ahora: datetime, limite: int, despues: Any
    ) -> list[ResumenCotizacion]:
        self.pedidos.append((filtros, limite, despues))
        filas = self.filas
        if despues is not None:
            filas = [f for f in filas if (f.creada_en, f.cotizacion_id) < despues]
        return filas[:limite]

    async def contar(self, filtros: FiltrosCotizaciones, ahora: datetime) -> int:
        return len(self.filas)


# --- dominio y caso de uso ---------------------------------------------------------------------


def test_el_estado_se_deriva_del_instante() -> None:
    fila = resumen(1, horas_vigencia=2)
    assert fila.estado(AHORA) is EstadoOferta.VIGENTE
    assert fila.estado(fila.vigente_hasta) is EstadoOferta.VENCIDA


def test_el_cursor_va_y_vuelve() -> None:
    fila = resumen(3)
    assert decodificar_cursor(codificar_cursor(fila)) == (fila.creada_en, fila.cotizacion_id)


@pytest.mark.parametrize(
    "cursor",
    [
        "no-es-base64!",
        base64.urlsafe_b64encode(b"sin-separador").decode(),
        base64.urlsafe_b64encode(b"2026-10-15T15:00:00|no-uuid").decode(),
        # Sin zona horaria: no lo entregó cotizacion.
        base64.urlsafe_b64encode(b"2026-10-15T15:00:00|" + str(UUID(int=1)).encode()).decode(),
    ],
)
def test_un_cursor_ajeno_es_invalido(cursor: str) -> None:
    with pytest.raises(CursorInvalido):
        decodificar_cursor(cursor)


async def test_pagina_por_cursor_sin_repetir_ni_saltar() -> None:
    repo = ListadoFalso([resumen(n) for n in range(1, 6)])
    caso = ListarCotizaciones(repo, RelojFijo())
    primera = await caso.ejecutar(FiltrosCotizaciones(), ASESOR, 2, None)
    assert [r.codigo for r in primera.items] == ["COT-2026-00001", "COT-2026-00002"]
    assert primera.total == 5 and primera.siguiente_cursor is not None
    segunda = await caso.ejecutar(FiltrosCotizaciones(), ASESOR, 2, primera.siguiente_cursor)
    assert [r.codigo for r in segunda.items] == ["COT-2026-00003", "COT-2026-00004"]
    ultima = await caso.ejecutar(FiltrosCotizaciones(), ASESOR, 2, segunda.siguiente_cursor)
    assert [r.codigo for r in ultima.items] == ["COT-2026-00005"]
    assert ultima.siguiente_cursor is None
    # Pide uno de más para saber si hay otra página.
    assert {limite for _, limite, _ in repo.pedidos} == {3}


async def test_un_socio_solo_ve_las_suyas_y_el_asesor_todas() -> None:
    repo = ListadoFalso([])
    caso = ListarCotizaciones(repo, RelojFijo())
    await caso.ejecutar(FiltrosCotizaciones(q="COT-", socio_id="SOC-999999"), SOCIO, 20, None)
    await caso.ejecutar(FiltrosCotizaciones(q="COT-"), ASESOR, 20, None)
    (socio, _, _), (asesor, _, _) = repo.pedidos
    assert (socio.socio_id, socio.q) == ("SOC-000001", "COT-")
    assert asesor.socio_id is None


async def test_un_cursor_invalido_no_consulta_la_base() -> None:
    repo = ListadoFalso([resumen(1)])
    with pytest.raises(CursorInvalido):
        await ListarCotizaciones(repo, RelojFijo()).ejecutar(FiltrosCotizaciones(), ASESOR, 20, "x")
    assert repo.pedidos == []


# --- repositorio PostgreSQL --------------------------------------------------------------------


def test_filtros_del_listado() -> None:
    assert filtros_listado(FiltrosCotizaciones(), AHORA) == ([], [])
    condiciones, parametros = filtros_listado(
        FiltrosCotizaciones(EstadoOferta.VIGENTE, " 2026_0% ", "SOC-000001"), AHORA
    )
    assert condiciones == ["o.vigente_hasta > %s", "c.socio_id = %s", "o.codigo ILIKE %s"]
    # El texto se escapa: `_` y `%` no son comodines.
    assert parametros == [AHORA, "SOC-000001", "%2026\\_0\\%%"]
    vencidas, _ = filtros_listado(FiltrosCotizaciones(EstadoOferta.VENCIDA, "  "), AHORA)
    assert vencidas == ["o.vigente_hasta <= %s"]


def test_la_fila_del_listado() -> None:
    fila = {
        "cotizacion_id": UUID(int=7),
        "codigo": "COT-2026-00007",
        "cliente_id": UUID(int=1),
        "socio_id": None,
        "producto_id": "vida-hipotecario",
        "version_producto": "2026.09.1",
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "suma_asegurada": "18000000",
        "precio": Decimal("15000.0000"),
        "nivel_riesgo": None,
        "degradada": True,
        "creada_en": AHORA,
        "vigente_hasta": AHORA + timedelta(days=7),
    }
    leida = resumen_de(fila)
    assert (leida.prima, leida.suma_asegurada) == (Decimal(15000), Decimal(18000000))
    assert leida.nivel_riesgo is None and leida.degradada
    assert resumen_de(fila | {"nivel_riesgo": "ALTO"}).nivel_riesgo is NivelRiesgo.ALTO


class Cursor:
    def __init__(self, filas: list[dict[str, Any]], total: int) -> None:
        self.filas = filas
        self.total = total
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any) -> None:
        self.sentencias.append((sql, parametros))

    async def fetchall(self) -> list[dict[str, Any]]:
        return self.filas

    async def fetchone(self) -> tuple[int] | None:
        return (self.total,) if self.total >= 0 else None


class Conexion:
    def __init__(self, cursor: Cursor) -> None:
        self.cursor_ = cursor

    def cursor(self, row_factory: Any) -> Cursor:
        return self.cursor_

    async def execute(self, sql: str, parametros: Any) -> Cursor:
        await self.cursor_.execute(sql, parametros)
        return self.cursor_


class Pool:
    def __init__(self, conexion: Conexion) -> None:
        self.conexion = conexion

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Conexion]:
        yield self.conexion


async def test_listar_ordena_pagina_y_cuenta() -> None:
    cursor = Cursor([], total=12)
    repo = CotizacionesPg(Pool(Conexion(cursor)))  # type: ignore[arg-type]
    despues = (AHORA, UUID(int=3))
    assert await repo.listar(FiltrosCotizaciones(EstadoOferta.VIGENTE), AHORA, 21, despues) == []
    assert await repo.contar(FiltrosCotizaciones(), AHORA) == 12
    (sql, parametros), (sql_contar, parametros_contar) = cursor.sentencias
    assert "FROM ofertas o JOIN cotizaciones c" in sql
    assert "(o.creada_en, c.id) < (%s, %s)" in sql
    assert sql.endswith("ORDER BY o.creada_en DESC, c.id DESC LIMIT %s")
    assert parametros == [AHORA, AHORA, UUID(int=3), 21]
    assert sql_contar.startswith("SELECT count(*) FROM ofertas") and "WHERE" not in sql_contar
    assert parametros_contar == []
    cursor.total = -1
    assert await repo.contar(FiltrosCotizaciones(), AHORA) == 0


# --- ruta GET /v1/cotizaciones -----------------------------------------------------------------


@pytest.fixture
def listado() -> ListadoFalso:
    return ListadoFalso(
        [
            resumen(1, degradada=True, nivel_riesgo=None),
            resumen(200, socio_id="SOC-000001"),  # creada hace 200 h: vencida (vigencia 168 h)
        ]
    )


def _app(escenario: Any, escenario_cotizacion: Any, listado: Any) -> TestClient:
    puertos = Puertos(
        identidad=escenario_cotizacion.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        ejecutor=escenario.ejecutor,
        perfiles=escenario_cotizacion.perfiles,
        reloj=escenario_cotizacion.reloj,
        productos=escenario_cotizacion.productos,
        cotizaciones=escenario_cotizacion.repo,
        listado=listado,
    )
    return TestClient(crear_app(Settings(), puertos=puertos))


@pytest.fixture
def api(escenario: Any, escenario_cotizacion: Any, listado: ListadoFalso) -> Iterator[TestClient]:
    with _app(escenario, escenario_cotizacion, listado) as cliente:
        yield cliente


def test_200_con_estado_y_montos_en_texto(api: TestClient, listado: ListadoFalso) -> None:
    respuesta = api.get(
        "/v1/cotizaciones",
        params={"estado": "VIGENTE", "q": "COT-2026", "limite": 1},
        headers=CABECERAS_ASESOR,
    )
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 2 and cuerpo["siguienteCursor"]
    (item,) = cuerpo["items"]
    assert item == {
        "cotizacionId": "00000000-0000-4000-d000-000000000001",
        "codigo": "COT-2026-00001",
        "clienteId": "00000000-0000-4000-8000-000000000001",
        "socioId": None,
        "productoId": "vida-hipotecario",
        "versionProducto": "2026.09.1",
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "sumaAsegurada": "180000000",
        "prima": "33264",
        "nivelRiesgo": None,
        "degradada": True,
        "creadaEn": "2026-10-15T14:00:00Z",
        "vigenteHasta": "2026-10-22T14:00:00Z",
        "estado": "VIGENTE",
    }
    ((filtros, _, _),) = listado.pedidos
    assert (filtros.estado, filtros.q) == (EstadoOferta.VIGENTE, "COT-2026")
    siguiente = api.get(
        "/v1/cotizaciones", params={"cursor": cuerpo["siguienteCursor"]}, headers=CABECERAS_ASESOR
    ).json()
    assert [i["estado"] for i in siguiente["items"]] == ["VENCIDA"]
    assert siguiente["siguienteCursor"] is None


def test_el_socio_lista_con_su_id(api: TestClient, listado: ListadoFalso) -> None:
    assert api.get("/v1/cotizaciones", headers=CABECERAS_SOCIO).status_code == 200
    assert listado.pedidos[0][0].socio_id == "SOC-000001"


@pytest.mark.parametrize(
    ("parametros", "encabezados", "estado", "codigo"),
    [
        ({}, {}, 401, "ACTOR_REQUERIDO"),
        ({"cursor": "x"}, CABECERAS_ASESOR, 422, "SOLICITUD_INVALIDA"),
        ({"estado": "ACEPTADA"}, CABECERAS_ASESOR, 422, "SOLICITUD_INVALIDA"),
        ({"limite": 101}, CABECERAS_ASESOR, 422, "SOLICITUD_INVALIDA"),
        ({"q": ""}, CABECERAS_ASESOR, 422, "SOLICITUD_INVALIDA"),
    ],
)
def test_solicitudes_rechazadas(
    api: TestClient,
    parametros: dict[str, Any],
    encabezados: dict[str, str],
    estado: int,
    codigo: str,
) -> None:
    respuesta = api.get("/v1/cotizaciones", params=parametros, headers=encabezados)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)


def test_sin_listado_configurado_responde_503(escenario: Any, escenario_cotizacion: Any) -> None:
    with _app(escenario, escenario_cotizacion, None) as api:
        respuesta = api.get("/v1/cotizaciones", headers=CABECERAS_ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "DEPENDENCIA_NO_DISPONIBLE")
