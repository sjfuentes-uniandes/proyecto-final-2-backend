"""N1 de HU-W05: presupuesto del recorrido, X-Deadline-Ms, timeout efectivo con y sin deadline,
cancelación real, reintento acotado por el presupuesto y registro del timeout."""

import asyncio
import json
import logging
import time

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from solventa_common import correlation, presupuesto
from solventa_common.logging import FormateadorJson
from solventa_common.resiliencia import (
    ProteccionDependencia,
    SinProteccion,
    TiempoAgotado,
    acotar,
    timeout_efectivo_ms,
)

pytestmark = pytest.mark.anyio

# Tolerancia del corte medido: el planificador de asyncio y la carga de la máquina de CI.
TOLERANCIA_MS = 60


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _status(codigo: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        str(codigo),
        request=httpx.Request("GET", "http://dependencia"),
        response=httpx.Response(codigo),
    )


# --- Presupuesto y encabezado --------------------------------------------------------------


async def test_sin_recorrido_no_hay_restante() -> None:
    assert presupuesto.actual() is None
    assert presupuesto.restante_ms() is None


async def test_iniciar_usa_el_propio_y_se_cierra_al_salir() -> None:
    with presupuesto.iniciar(700) as recorrido:
        assert recorrido.total_ms == 700
        assert 690 <= presupuesto.restante_ms() <= 700  # type: ignore[operator]
    assert presupuesto.restante_ms() is None


@pytest.mark.parametrize(("recibido", "esperado"), [(300, 300), (900, 700), (None, 700)])
async def test_el_recibido_acota_el_propio(recibido: int | None, esperado: int) -> None:
    with presupuesto.iniciar(700, recibido) as recorrido:
        assert recorrido.total_ms == esperado


async def test_el_restante_disminuye_y_nunca_es_negativo() -> None:
    with presupuesto.iniciar(30):
        await asyncio.sleep(0.05)
        assert presupuesto.restante_ms() == 0


async def test_el_presupuesto_es_por_tarea() -> None:
    async def dentro() -> int | None:
        with presupuesto.iniciar(100):
            await asyncio.sleep(0)
            return presupuesto.restante_ms()

    with presupuesto.iniciar(700):
        otro = await asyncio.create_task(dentro())
        assert otro is not None and otro <= 100
        assert presupuesto.actual().total_ms == 700  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [("250", 250), (" 40 ", 40), ("0", None), ("-5", None), ("abc", None), (None, None)],
)
async def test_leer_encabezado(valor: str | None, esperado: int | None) -> None:
    assert presupuesto.leer_encabezado(valor) == esperado


async def test_encabezado_es_el_restante_en_ms_enteros() -> None:
    assert presupuesto.encabezado(279.9) == {"X-Deadline-Ms": "279"}
    assert presupuesto.encabezado(0.2) == {"X-Deadline-Ms": "1"}


async def test_por_operacion_lee_el_json_de_latency_budget_ms() -> None:
    assert presupuesto.por_operacion('{"cotizacion": 700, "socio.cotizar": 1000}') == {
        "cotizacion": 700,
        "socio.cotizar": 1000,
    }
    assert presupuesto.por_operacion(None) == {}
    assert presupuesto.por_operacion("") == {}


@pytest.mark.parametrize("valor", ['{"cotizacion": 0}', '{"cotizacion": "700"}', "[700]"])
async def test_por_operacion_rechaza_valores_invalidos(valor: str) -> None:
    with pytest.raises(ValueError):
        presupuesto.por_operacion(valor)


async def test_validar_timeouts_exige_menor_que_el_presupuesto() -> None:
    presupuesto.validar_timeouts("cotizacion", 700, {"catalogo": 300, "clientes": 699})
    with pytest.raises(ValueError, match="catalogo=700"):
        presupuesto.validar_timeouts("cotizacion", 700, {"catalogo": 700, "clientes": 300})


# --- Timeout efectivo con y sin deadline ---------------------------------------------------


async def test_timeout_efectivo_sin_deadline_es_el_configurado() -> None:
    assert timeout_efectivo_ms(300, None) == 300


async def test_timeout_efectivo_con_deadline_del_recorrido() -> None:
    with presupuesto.iniciar(200):
        assert timeout_efectivo_ms(300, presupuesto.restante_ms()) <= 180


# --- Cancelación real dentro del límite ------------------------------------------------------


@pytest.mark.parametrize("proteccion", [SinProteccion(), ProteccionDependencia(5, 30, 10)])
async def test_la_llamada_lenta_se_abandona_dentro_del_limite(proteccion: object) -> None:
    cancelada = asyncio.Event()

    async def lenta(_: float) -> None:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelada.set()
            raise

    antes = asyncio.all_tasks()
    inicio = time.perf_counter()
    with pytest.raises(TiempoAgotado):
        await proteccion.ejecutar("F", "G", lenta, 150, None)  # type: ignore[attr-defined]
    duracion_ms = (time.perf_counter() - inicio) * 1000
    assert 150 <= duracion_ms <= 150 + TOLERANCIA_MS, duracion_ms
    assert cancelada.is_set()
    # No queda ninguna tarea colgando de la llamada abandonada.
    assert asyncio.all_tasks() - antes == set()


async def test_acotar_corta_con_el_deadline_recibido() -> None:
    async def lenta() -> None:
        await asyncio.sleep(5)

    inicio = time.perf_counter()
    with pytest.raises(TiempoAgotado):
        await acotar(lenta, operacion="op", proveedor="base", configurado_ms=500, deadline_ms=120)
    assert (time.perf_counter() - inicio) * 1000 <= 100 + TOLERANCIA_MS


async def test_acotar_devuelve_el_resultado_a_tiempo() -> None:
    async def rapida() -> str:
        return "ok"

    assert (
        await acotar(rapida, operacion="op", proveedor="base", configurado_ms=500, deadline_ms=None)
        == "ok"
    )


# --- Reintento: solo recuperable y solo si queda presupuesto ---------------------------------


async def test_5xx_reintenta_si_queda_presupuesto() -> None:
    llamadas: list[float] = []

    async def caida_una_vez(timeout_s: float) -> str:
        llamadas.append(timeout_s)
        if len(llamadas) == 1:
            raise _status(503)
        return "ok"

    with presupuesto.iniciar(700):
        assert (
            await ProteccionDependencia(5, 30, 10).ejecutar(
                "F", "G", caida_una_vez, 300, presupuesto.restante_ms()
            )
            == "ok"
        )
    assert len(llamadas) == 2
    # El reintento usa lo que queda de la misma ventana, no una nueva.
    assert llamadas[1] < llamadas[0] - 0.02


@pytest.mark.parametrize("error", [httpx.ReadTimeout("lento"), _status(404)])
async def test_un_timeout_o_un_4xx_no_reintentan(error: Exception) -> None:
    llamadas: list[int] = []

    async def falla(_: float) -> None:
        llamadas.append(1)
        raise error

    with pytest.raises(Exception):  # noqa: B017 - el tipo varía por caso
        await ProteccionDependencia(5, 30, 10).ejecutar("F", "G", falla, 300, None)
    assert len(llamadas) == 1


async def test_presupuesto_agotado_no_reintenta() -> None:
    llamadas: list[int] = []

    async def caida(_: float) -> None:
        llamadas.append(1)
        raise _status(503)

    # Restante 120 ms: tras la espera de 25 ms quedarían < 100 ms, el mínimo para reintentar.
    with presupuesto.iniciar(120), pytest.raises(Exception):  # noqa: B017
        await ProteccionDependencia(5, 30, 10).ejecutar(
            "F", "G", caida, 300, presupuesto.restante_ms()
        )
    assert len(llamadas) == 1


async def test_backoff_y_presupuesto_del_recorrido_con_varias_dependencias() -> None:
    """AC2: una dependencia consume su tiempo máximo; la siguiente no reintenta más allá del
    presupuesto y el recorrido termina dentro de él."""
    proteccion = ProteccionDependencia(5, 30, 10)
    intentos: list[float] = []

    async def lenta(_: float) -> None:
        await asyncio.sleep(5)

    async def caida(_: float) -> None:
        intentos.append(time.perf_counter())
        raise _status(503)

    inicio = time.perf_counter()
    with presupuesto.iniciar(400):
        with pytest.raises(TiempoAgotado):
            await proteccion.ejecutar("A", "G1", lenta, 300, presupuesto.restante_ms())
        with pytest.raises(Exception):  # noqa: B017
            await proteccion.ejecutar("B", "G2", caida, 300, presupuesto.restante_ms())
    total_ms = (time.perf_counter() - inicio) * 1000
    assert len(intentos) == 1  # quedaban < 125 ms: sin reintento
    assert total_ms <= 400 + TOLERANCIA_MS, total_ms


# --- AC1: registro del timeout y atributos de la traza --------------------------------------


async def test_el_timeout_se_registra_con_proveedor_operacion_y_correlacion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def lenta(_: float) -> None:
        await asyncio.sleep(5)

    token = correlation.establecer("corr-hu-w05")
    try:
        with (
            caplog.at_level(logging.WARNING, logger="solventa.resiliencia"),
            presupuesto.iniciar(400),
            pytest.raises(TiempoAgotado),
        ):
            await SinProteccion().ejecutar(
                "catalogo.producto", "catalogo", lenta, 100, presupuesto.restante_ms()
            )
        registros = [r for r in caplog.records if r.message == "dependencia agotó su tiempo"]
        assert len(registros) == 1
        linea = json.loads(FormateadorJson("pruebas", "local").format(registros[0]))
    finally:
        correlation.restablecer(token)
    assert linea["level"] == "WARNING"
    assert linea["correlation_id"] == "corr-hu-w05"
    assert linea["proveedor"] == "catalogo"
    assert linea["operacion"] == "catalogo.producto"
    assert linea["timeout_ms"] == 100
    assert 250 <= linea["restante_ms"] <= 300


async def test_la_llamada_anota_presupuesto_restante_y_timeout_en_el_span() -> None:
    exportador = InMemorySpanExporter()
    proveedor = TracerProvider()
    proveedor.add_span_processor(SimpleSpanProcessor(exportador))
    tracer = proveedor.get_tracer("pruebas")

    async def lenta(_: float) -> None:
        await asyncio.sleep(5)

    with (
        tracer.start_as_current_span("cotizacion"),
        presupuesto.iniciar(500),
        tracer.start_as_current_span("cotizacion.dependencia.catalogo.producto"),
        pytest.raises(TiempoAgotado),
    ):
        await ProteccionDependencia(5, 30, 10).ejecutar(
            "catalogo.producto", "catalogo", lenta, 100, presupuesto.restante_ms()
        )
    spans = {s.name: dict(s.attributes or {}) for s in exportador.get_finished_spans()}
    dependencia = spans["cotizacion.dependencia.catalogo.producto"]
    assert dependencia["solventa.presupuesto_ms"] == 500
    assert dependencia["solventa.timeout_ms"] == 100
    assert 480 <= dependencia["solventa.restante_ms"] <= 500
    assert spans["cotizacion"]["solventa.presupuesto_ms"] == 500


async def test_sin_deadline_solo_se_anota_el_timeout() -> None:
    exportador = InMemorySpanExporter()
    proveedor = TracerProvider()
    proveedor.add_span_processor(SimpleSpanProcessor(exportador))

    async def rapida(_: float) -> str:
        return "ok"

    with proveedor.get_tracer("pruebas").start_as_current_span("dep"):
        await SinProteccion().ejecutar("F", "G", rapida, 300, None)
    (span,) = exportador.get_finished_spans()
    assert dict(span.attributes or {}) == {"solventa.timeout_ms": 300}
