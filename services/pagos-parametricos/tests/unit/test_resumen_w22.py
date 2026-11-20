"""El seguimiento de H09 y la lectura de las activaciones (HU-W22, W22-B09): el caso de uso, las
consultas, las rutas y el cableado. El comportamiento contra PostgreSQL real está en `tests/module`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Self
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pagos_parametricos.adapters.outbound.activaciones_pg import (
    ActivacionesPg,
    activacion_de,
    disparador_a_json,
)
from pagos_parametricos.adapters.outbound.dlq_sqs import DlqSqs
from pagos_parametricos.adapters.outbound.resumen_pg import ResumenPg, pago_de
from pagos_parametricos.application.parametrico.resumen import (
    MINUTOS_DE_RITMO,
    ConsultarResumen,
    DatosDeOrigen,
    OrigenReciente,
    PagoActivado,
    ritmo_completo,
)
from pagos_parametricos.domain.parametrico.activacion import Disparador, SiniestroCreado
from pagos_parametricos.main import crear_app

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 11, 10, 14, 7, 31, tzinfo=UTC)
EVENTO = UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09")
POLIZA = UUID("b2100000-0000-4000-8000-000000000101")
SINIESTRO = UUID("5a000000-0000-4000-8000-000000000022")
ANALISTA = {
    "X-Actor-Id": "a0a0a0a0-0000-4000-8000-000000000001",
    "X-Actor-Rol": "ANALISTA_SINIESTROS",
}
DISPARADOR = {
    "tipoEvento": "LLUVIA_ACUMULADA",
    "valorObservado": "135",
    "umbral": "120.000",
    "unidad": "mm",
}


# --- Conexión falsa ---------------------------------------------------------------------------
class Cursor:
    def __init__(self, filas: list[tuple[Any, ...]]) -> None:
        self.filas = filas

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.filas[0] if self.filas else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self.filas


class Conexion:
    def __init__(self, *respuestas: list[tuple[Any, ...]]) -> None:
        self.respuestas = list(respuestas)
        self.sentencias: list[tuple[str, Any]] = []
        self.transacciones = 0

    async def execute(self, sql: str, parametros: Any = None) -> Cursor:
        self.sentencias.append((sql, parametros))
        return Cursor(self.respuestas.pop(0) if self.respuestas else [])

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Self]:
        self.transacciones += 1
        yield self


class Pool:
    def __init__(self, conexion: Conexion) -> None:
        self.conexion = conexion
        self.prestadas = 0

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Conexion]:
        self.prestadas += 1
        yield self.conexion


# --- El ritmo --------------------------------------------------------------------------------------
def test_el_ritmo_son_los_ultimos_15_minutos_completos_con_ceros() -> None:
    actual = AHORA.replace(second=0, microsecond=0)

    ritmo = ritmo_completo(
        {actual: 7, actual - timedelta(minutes=3): 2, actual - timedelta(hours=1): 99}, AHORA
    )

    assert len(ritmo) == MINUTOS_DE_RITMO == 15
    assert ritmo[0].minuto == actual - timedelta(minutes=14) and ritmo[-1].minuto == actual
    assert [m.procesados for m in ritmo].count(0) == 13
    assert (ritmo[-1].procesados, ritmo[-4].procesados) == (7, 2)  # el de hace una hora no cuenta


# --- El caso de uso ----------------------------------------------------------------------------------
class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


class LecturaFalsa:
    def __init__(self, origen: str | None = "EVT-2026-0913") -> None:
        self.origen = origen
        self.consultas: list[tuple[str, datetime]] = []

    async def origen_mas_reciente(self) -> str | None:
        return self.origen

    async def datos(self, evento_origen: str, desde_ritmo: datetime) -> DatosDeOrigen:
        self.consultas.append((evento_origen, desde_ritmo))
        pago = PagoActivado(
            SINIESTRO, "88-2026-1001", POLIZA, "250-2026-000101",
            Disparador("LLUVIA_ACUMULADA", "135", "120.000", "mm"), "1400000.00", "COP", "SINIESTRO_CREADO",
        )  # fmt: skip
        return DatosDeOrigen(AHORA - timedelta(minutes=6), 90, 4, 12, {}, (pago,))

    async def origenes(self, limite: int) -> list[OrigenReciente]:
        return [OrigenReciente("EVT-2026-0913", AHORA - timedelta(minutes=6), AHORA, 90)][:limite]


class DlqFalsa:
    def __init__(self, cantidad: int | Exception) -> None:
        self.cantidad = cantidad

    async def profundidad(self) -> int:
        if isinstance(self.cantidad, Exception):
            raise self.cantidad
        return self.cantidad


async def test_el_resumen_define_recibidos_como_procesados_mas_duplicados() -> None:
    caso = ConsultarResumen(LecturaFalsa(), DlqFalsa(3), RelojFijo())

    r = await caso.resumen(None)

    assert (r.evento_origen, r.procesados, r.duplicados, r.recibidos, r.activados, r.en_dlq) == (
        "EVT-2026-0913", 90, 4, 94, 12, 3,
    )  # fmt: skip
    assert len(r.ritmo_por_minuto) == 15 and len(r.ultimos_pagos) == 1


async def test_con_un_origen_pedido_no_busca_el_mas_reciente_y_el_ritmo_arranca_hace_14_minutos() -> (
    None
):
    lectura = LecturaFalsa(origen=None)

    r = await ConsultarResumen(lectura, None, RelojFijo()).resumen("EVT-OTRO")

    assert lectura.consultas == [("EVT-OTRO", AHORA.replace(second=0) - timedelta(minutes=14))]
    assert r.evento_origen == "EVT-OTRO" and r.en_dlq is None


async def test_sin_eventos_todo_va_en_cero_y_el_ritmo_viene_completo() -> None:
    r = await ConsultarResumen(LecturaFalsa(origen=None), DlqFalsa(0), RelojFijo()).resumen(None)

    assert (r.evento_origen, r.desde, r.recibidos, r.procesados, r.activados) == (
        None,
        None,
        0,
        0,
        0,
    )
    assert (r.ultimos_pagos, r.en_dlq) == ((), 0)
    assert [m.procesados for m in r.ritmo_por_minuto] == [0] * 15


async def test_si_no_se_puede_leer_la_dlq_en_dlq_es_null_y_el_resumen_no_falla(
    telemetria: Any,
) -> None:
    r = await ConsultarResumen(
        LecturaFalsa(), DlqFalsa(PermissionError("sin permiso")), RelojFijo()
    ).resumen(None)

    assert r.en_dlq is None and r.procesados == 90
    assert "enDlq" in telemetria.texto_logs() and "sin permiso" not in telemetria.texto_logs()


async def test_los_origenes_se_acotan_al_limite() -> None:
    assert len(await ConsultarResumen(LecturaFalsa(), None, RelojFijo()).origenes(1)) == 1


async def test_la_medicion_es_parametrico_resumen(telemetria: Any) -> None:
    await ConsultarResumen(LecturaFalsa(), None, RelojFijo()).resumen(None)
    await ConsultarResumen(LecturaFalsa(), None, RelojFijo()).origenes(5)

    nombres = [s.name for s in telemetria.lista_spans()]
    assert nombres.count("parametrico.resumen") == 2


# --- La DLQ -----------------------------------------------------------------------------------------
async def test_la_profundidad_de_la_dlq_se_lee_con_get_queue_attributes() -> None:
    class Sqs:
        def get_queue_attributes(self, **argumentos: Any) -> dict[str, Any]:
            self.argumentos = argumentos
            return {"Attributes": {"ApproximateNumberOfMessages": "3"}}

    sqs = Sqs()
    dlq = DlqSqs(sqs, "http://l/000000000000/solventa-local-parametricos-eventos")

    assert await dlq.profundidad() == 3
    assert sqs.argumentos == {
        "QueueUrl": "http://l/000000000000/solventa-local-parametricos-eventos-dlq",
        "AttributeNames": ["ApproximateNumberOfMessages"],
    }


# --- ResumenPg -----------------------------------------------------------------------------------------
async def test_resumen_pg_lee_por_origen_con_los_indices_y_una_conexion_por_metodo() -> None:
    minuto = AHORA.replace(second=0, microsecond=0)
    conexion = Conexion(
        [("EVT-2026-0913",)],
        [(90, 4, minuto - timedelta(minutes=6))],
        [(12,)],
        [(minuto, 5), (minuto - timedelta(minutes=2), 3)],
        [
            (
                SINIESTRO,
                "88-2026-1001",
                POLIZA,
                "250-2026-000101",
                DISPARADOR,
                Decimal("1400000.00"),
                "COP ",
                "SINIESTRO_CREADO",
            )
        ],
    )
    pool = Pool(conexion)
    lectura = ResumenPg(pool)

    assert await lectura.origen_mas_reciente() == "EVT-2026-0913"
    datos = await lectura.datos("EVT-2026-0913", minuto - timedelta(minutes=14))

    assert (datos.procesados, datos.duplicados, datos.activados) == (90, 4, 12)
    assert datos.por_minuto == {minuto: 5, minuto - timedelta(minutes=2): 3}
    (pago,) = datos.ultimos_pagos
    assert (pago.siniestro_id, pago.siniestro_codigo, pago.valor, pago.moneda) == (
        SINIESTRO, "88-2026-1001", "1400000.00", "COP",
    )  # fmt: skip
    assert pago.disparador == Disparador("LLUVIA_ACUMULADA", "135", "120.000", "mm")
    assert pool.prestadas == 2
    sql = [s for s, _ in conexion.sentencias]
    assert "evento_origen = %s" in sql[1] and "evaluado_en >= %s" in sql[3]
    assert "estado = 'SINIESTRO_CREADO'" in sql[4] and "LIMIT 10" in sql[4]


async def test_resumen_pg_sin_eventos_y_los_origenes() -> None:
    assert await ResumenPg(Pool(Conexion([]))).origen_mas_reciente() is None
    desde, hasta = AHORA - timedelta(minutes=6), AHORA
    conexion = Conexion([("EVT-2026-0913", desde, hasta, 90)])

    origenes = await ResumenPg(Pool(conexion)).origenes(10)

    assert origenes == [OrigenReciente("EVT-2026-0913", desde, hasta, 90)]
    assert (
        conexion.sentencias[0][1] == (10,) and "GROUP BY evento_origen" in conexion.sentencias[0][0]
    )


def test_el_pago_activado_se_arma_de_la_fila() -> None:
    fila = (
        SINIESTRO,
        "88-2026-1001",
        POLIZA,
        None,
        DISPARADOR,
        Decimal("620000.00"),
        "COP",
        "SINIESTRO_CREADO",
    )

    p = pago_de(fila)

    assert (p.poliza_numero, p.valor, p.estado_activacion) == (
        None,
        "620000.00",
        "SINIESTRO_CREADO",
    )


# --- ActivacionesPg --------------------------------------------------------------------------------------
async def test_las_pendientes_se_leen_con_la_cobertura_de_su_poliza() -> None:
    desde, hasta = AHORA, AHORA + timedelta(days=365)
    cliente = UUID(int=9)
    conexion = Conexion(
        [
            (
                EVENTO,
                POLIZA,
                Decimal("1400000.00"),
                "COP ",
                "1",
                cliente,
                "250-2026-000101",
                "Paramétrico",
                Decimal("120.000"),
                desde,
                hasta,
            )
        ]
    )
    pool = Pool(conexion)

    (p,) = await ActivacionesPg(pool).pendientes(EVENTO)

    assert (p.cliente_id, p.moneda, p.umbral, p.ramo) == (
        cliente,
        "COP",
        Decimal("120.000"),
        "Paramétrico",
    )
    sql, parametros = conexion.sentencias[0]
    assert "LEFT JOIN coberturas_activas" in sql and "estado = 'PENDIENTE'" in sql
    assert parametros == (EVENTO,) and pool.prestadas == 1


def test_una_pendiente_sin_cobertura_trae_todo_nulo_y_se_lee_igual() -> None:
    fila = (EVENTO, POLIZA, Decimal("1.00"), "COP", "1", None, None, None, None, None, None)

    p = activacion_de(fila)

    assert (p.cliente_id, p.numero, p.vigente_desde) == (None, None, None)


async def test_marcar_creada_actualiza_solo_la_pendiente_en_una_transaccion_corta() -> None:
    conexion = Conexion()
    pool = Pool(conexion)
    disparador = Disparador("LLUVIA_ACUMULADA", "135", "120.000", "mm")

    await ActivacionesPg(pool).marcar_creada(
        EVENTO, POLIZA, SiniestroCreado(SINIESTRO, "88-2026-1001", True), disparador
    )

    [(sql, parametros)] = conexion.sentencias
    assert "SET estado = 'SINIESTRO_CREADO'" in sql and "AND estado = 'PENDIENTE'" in sql
    assert parametros[:2] == (SINIESTRO, "88-2026-1001") and parametros[3:] == (EVENTO, POLIZA)
    assert parametros[2].obj == disparador_a_json(disparador) == DISPARADOR | {"umbral": "120.000"}
    assert conexion.transacciones == 1


async def test_sumar_duplicado_es_un_update_corto() -> None:
    conexion = Conexion()

    await ActivacionesPg(Pool(conexion)).sumar_duplicado(EVENTO)

    [(sql, parametros)] = conexion.sentencias
    assert sql.startswith("UPDATE eventos_evaluados SET duplicados = duplicados + 1")
    assert parametros == (EVENTO,) and conexion.transacciones == 1


# --- Las rutas ------------------------------------------------------------------------------------------
@pytest.fixture
def cliente() -> TestClient:
    app = crear_app()
    app.state.resumen = ConsultarResumen(LecturaFalsa(), DlqFalsa(3), RelojFijo())
    return TestClient(app)


def test_el_resumen_responde_con_el_contrato(cliente: TestClient) -> None:
    respuesta = cliente.get(
        "/v1/parametricos/resumen", params={"eventoOrigen": "EVT-2026-0913"}, headers=ANALISTA
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert (cuerpo["eventoOrigen"], cuerpo["recibidos"], cuerpo["procesados"], cuerpo["enDlq"]) == (
        "EVT-2026-0913", 94, 90, 3,
    )  # fmt: skip
    assert len(cuerpo["ritmoPorMinuto"]) == 15
    assert cuerpo["ultimosPagos"][0] == {
        "siniestroId": str(SINIESTRO),
        "siniestroCodigo": "88-2026-1001",
        "polizaId": str(POLIZA),
        "polizaNumero": "250-2026-000101",
        "disparador": {
            "tipoEvento": "LLUVIA_ACUMULADA",
            "valorObservado": "135",
            "umbral": "120.000",
            "unidad": "mm",
        },
        "valor": "1400000.00",
        "moneda": "COP",
        "estadoActivacion": "SINIESTRO_CREADO",
    }


def test_los_origenes_recientes(cliente: TestClient) -> None:
    respuesta = cliente.get(
        "/v1/parametricos/eventos-origen", params={"limite": 5}, headers=ANALISTA
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["items"][0]["eventoOrigen"] == "EVT-2026-0913"


@pytest.mark.parametrize(
    ("ruta", "params"),
    [
        ("/v1/parametricos/resumen", {"eventoOrigen": "con espacios"}),
        ("/v1/parametricos/eventos-origen", {"limite": 0}),
        ("/v1/parametricos/eventos-origen", {"limite": 51}),
    ],
)
def test_un_parametro_invalido_es_422(
    cliente: TestClient, ruta: str, params: dict[str, Any]
) -> None:
    assert cliente.get(ruta, params=params, headers=ANALISTA).status_code == 422


@pytest.mark.parametrize("ruta", ["/v1/parametricos/resumen", "/v1/parametricos/eventos-origen"])
def test_sin_actor_es_401_y_con_otro_rol_403(cliente: TestClient, ruta: str) -> None:
    assert cliente.get(ruta).status_code == 401
    otro = cliente.get(ruta, headers={"X-Actor-Id": "x", "X-Actor-Rol": "CLIENTE"})
    assert (otro.status_code, otro.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")


@pytest.mark.parametrize("ruta", ["/v1/parametricos/resumen", "/v1/parametricos/eventos-origen"])
def test_sin_base_las_rutas_responden_503(ruta: str) -> None:
    respuesta = TestClient(crear_app()).get(ruta, headers=ANALISTA)

    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "DEPENDENCIA_NO_DISPONIBLE")
