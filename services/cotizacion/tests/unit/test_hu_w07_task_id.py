"""HU-W07 (paso 7.5): `solventa.task_id` en el span `cotizacion`.

- Adaptador (`adapters/outbound/metadatos_ecs.py`): GET `${ECS_CONTAINER_METADATA_URI_V4}/task`
  con un tope total de 1 s, sin reintentos; devuelve el último segmento de `TaskARN` o None, y
  nunca lanza. Supuesto (documentación de ECS, endpoint de metadatos v4): Fargate inyecta la
  variable en cada contenedor y `/task` responde un JSON con `TaskARN`
  (`arn:aws:ecs:<región>:<cuenta>:task/<clúster>/<id>`).
- Settings: la variable estándar, sin prefijo (alias exacto); `SOLVENTA_` no la reemplaza.
- `ciclo` la lee una vez al arrancar, en todas las ramas, y la pasa a `ConfigCotizacion`. Si los
  metadatos no responden, la app arranca igual y no hay atributo.
- `CalcularPrima.ejecutar` la pone en el span `cotizacion`, también en la repetición idempotente.
  No va en la respuesta ni en los logs.
"""

import asyncio
import json
import socket
import time
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx
from cotizacion.adapters.outbound import metadatos_ecs
from cotizacion.adapters.outbound.metadatos_ecs import TIMEOUT_S, leer_task_id
from cotizacion.application.cotizacion.calcular_prima import ATRIBUTO_TASK_ID, OPERACION
from cotizacion.config import Settings
from cotizacion.main import Puertos, crear_app
from fastapi.testclient import TestClient

URI = "http://169.254.170.2/v4/00000000-sintetico"
TASK_ID = "0123456789abcdef0123456789abcdef"
TASK_ARN = f"arn:aws:ecs:us-east-1:000000000000:task/solventa-int/{TASK_ID}"
VARIABLE = "ECS_CONTAINER_METADATA_URI_V4"
ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
C01 = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosAsegurado": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
}


def puerto_cerrado() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto: int = s.getsockname()[1]
    return puerto


# --- Adaptador -------------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock(assert_all_called=True)
async def test_devuelve_el_ultimo_segmento_del_task_arn(respx_mock: Any) -> None:
    ruta = respx_mock.get(f"{URI}/task").respond(200, json={"TaskARN": TASK_ARN, "Cluster": "x"})
    assert await leer_task_id(URI) == TASK_ID
    assert ruta.call_count == 1


@pytest.mark.anyio
@respx.mock
async def test_el_formato_antiguo_del_arn_tambien(respx_mock: Any) -> None:
    respx_mock.get(f"{URI}/task").respond(200, json={"TaskARN": f"arn:aws:ecs:r:0:task/{TASK_ID}"})
    assert await leer_task_id(URI) == TASK_ID


@pytest.mark.anyio
@pytest.mark.parametrize("uri", [None, "", "   "])
@respx.mock(assert_all_mocked=True)
async def test_sin_variable_no_llama(respx_mock: Any, uri: str | None) -> None:
    assert await leer_task_id(uri) is None
    assert respx_mock.calls.call_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(500, json={"TaskARN": TASK_ARN}),
        httpx.Response(404),
        httpx.Response(200, content=b"no es json"),
        httpx.Response(200, json={"Cluster": "solventa-int"}),
        httpx.Response(200, json={"TaskARN": ""}),
        httpx.Response(200, json={"TaskARN": "arn:aws:ecs:r:0:task/"}),
        httpx.Response(200, json={"TaskARN": "arn-sin-segmentos"}),
        httpx.Response(200, json={"TaskARN": 123}),
        httpx.Response(200, json=[TASK_ARN]),
    ],
    ids=[
        "5xx",
        "404",
        "json-invalido",
        "sin-task-arn",
        "arn-vacio",
        "arn-sin-id",
        "arn-sin-barra",
        "arn-no-texto",
        "no-es-objeto",
    ],
)
@respx.mock
async def test_respuestas_invalidas_dan_none(respx_mock: Any, respuesta: httpx.Response) -> None:
    ruta = respx_mock.get(f"{URI}/task").mock(return_value=respuesta)
    assert await leer_task_id(URI) is None
    assert ruta.call_count == 1, "sin reintentos"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [httpx.ConnectError("x"), httpx.ReadTimeout("x"), RuntimeError("inesperado")]
)
@respx.mock
async def test_errores_de_red_o_inesperados_dan_none(respx_mock: Any, error: Exception) -> None:
    ruta = respx_mock.get(f"{URI}/task").mock(side_effect=error)
    assert await leer_task_id(URI) is None
    assert ruta.call_count == 1, "sin reintentos"


@pytest.mark.anyio
async def test_uri_malformada_da_none() -> None:
    assert await leer_task_id("no es una url") is None


@pytest.mark.anyio
@respx.mock
async def test_una_respuesta_lenta_se_corta_en_el_tope_total(respx_mock: Any) -> None:
    async def lenta(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"TaskARN": TASK_ARN})

    respx_mock.get(f"{URI}/task").mock(side_effect=lenta)
    inicio = time.monotonic()
    assert await leer_task_id(URI, timeout_s=0.05) is None
    assert time.monotonic() - inicio < 1


@pytest.mark.anyio
@respx.mock
async def test_el_timeout_por_omision_es_de_un_segundo_o_menos(respx_mock: Any) -> None:
    ruta = respx_mock.get(f"{URI}/task").respond(200, json={"TaskARN": TASK_ARN})
    await leer_task_id(URI)
    assert 0 < TIMEOUT_S <= 1
    tiempos = ruta.calls.last.request.extensions["timeout"]
    assert set(tiempos) == {"connect", "read", "write", "pool"}
    assert all(0 < valor <= 1 for valor in tiempos.values()), tiempos


def test_la_lectura_no_deja_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin solicitud padre, el GET de httpx (instrumentado de forma global) sería una traza
    suelta por tarea en X-Ray. Control: un GET fuera del adaptador sí deja su span."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    spans = InMemorySpanExporter()
    with respx.mock(assert_all_called=True) as metadatos:
        metadatos.get(f"{URI}/task").respond(200, json={"TaskARN": TASK_ARN})
        app = crear_app(Settings(**{VARIABLE: URI}), span_exporter=spans)
        with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}):
            assert spans.get_finished_spans() == (), [s.name for s in spans.get_finished_spans()]
            assert asyncio.run(leer_task_id(URI), loop_factory=asyncio.SelectorEventLoop) == TASK_ID
            assert spans.get_finished_spans() == ()

            async def control() -> None:
                async with httpx.AsyncClient() as cliente:
                    await cliente.get(f"{URI}/task")

            asyncio.run(control(), loop_factory=asyncio.SelectorEventLoop)
    assert [(s.name, s.kind.name) for s in spans.get_finished_spans()] == [("GET", "CLIENT")]


def test_el_adaptador_no_tiene_estado_de_modulo() -> None:
    nombres = {n for n in vars(metadatos_ecs) if not n.startswith("__")}
    assert {n for n in nombres if isinstance(getattr(metadatos_ecs, n), (dict, list, set))} == set()


# --- Settings: la variable estándar, sin prefijo --------------------------------------------


def test_settings_lee_la_variable_estandar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VARIABLE, URI)
    monkeypatch.delenv(f"SOLVENTA_{VARIABLE}", raising=False)
    assert Settings().ecs_container_metadata_uri_v4 == URI


def test_settings_no_la_toma_con_prefijo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VARIABLE, raising=False)
    monkeypatch.setenv(f"SOLVENTA_{VARIABLE}", URI)
    assert Settings().ecs_container_metadata_uri_v4 is None


def test_settings_sin_variable_es_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VARIABLE, raising=False)
    assert Settings().ecs_container_metadata_uri_v4 is None
    assert "ecs_container_metadata_uri_v4" not in repr(Settings(**{VARIABLE: URI}))


# --- Span de la cotización -------------------------------------------------------------------


def _spans_cotizacion(telemetria: Any) -> list[Any]:
    return [s for s in telemetria.spans.get_finished_spans() if s.name == OPERACION]


@pytest.mark.anyio
async def test_el_span_cotizacion_lleva_el_task_id_tambien_al_repetir(
    escenario_cotizacion: Any, nueva_cotizacion: Any, telemetria: Any
) -> None:
    caso = escenario_cotizacion.caso()
    caso.config = replace(caso.config, task_id=TASK_ID)
    _, _, creada = await caso.ejecutar(nueva_cotizacion())
    _, _, repetida_creada = await caso.ejecutar(nueva_cotizacion())
    assert (creada, repetida_creada) == (True, False)

    spans = _spans_cotizacion(telemetria)
    assert [s.attributes.get(ATRIBUTO_TASK_ID) for s in spans] == [TASK_ID, TASK_ID]
    assert ATRIBUTO_TASK_ID == "solventa.task_id"
    # Solo en el span cotizacion, no en las dependencias, ni en los logs.
    otros = [s for s in telemetria.spans.get_finished_spans() if s.name != OPERACION]
    assert otros and all(ATRIBUTO_TASK_ID not in (s.attributes or {}) for s in otros)
    assert TASK_ID not in telemetria.salida.getvalue()


@pytest.mark.anyio
async def test_sin_task_id_el_span_no_lo_lleva(
    escenario_cotizacion: Any, nueva_cotizacion: Any, telemetria: Any
) -> None:
    caso = escenario_cotizacion.caso()
    assert caso.config.task_id is None
    await caso.ejecutar(nueva_cotizacion())
    (span,) = _spans_cotizacion(telemetria)
    assert ATRIBUTO_TASK_ID not in (span.attributes or {})


@pytest.mark.anyio
async def test_un_rechazo_tambien_lleva_el_task_id(
    escenario_cotizacion: Any, nueva_cotizacion: Any, telemetria: Any
) -> None:
    caso = escenario_cotizacion.caso()
    caso.config = replace(caso.config, task_id=TASK_ID)
    with pytest.raises(Exception):  # noqa: B017 - el tipo no importa aquí
        await caso.ejecutar(nueva_cotizacion(producto_id="vida-hipotecario-2025"))
    (span,) = _spans_cotizacion(telemetria)
    assert span.attributes.get(ATRIBUTO_TASK_ID) == TASK_ID


# --- Arranque: ciclo lo lee una vez, en todas las ramas --------------------------------------


@pytest.fixture
def puertos(escenario: Any, escenario_cotizacion: Any) -> Puertos:
    return Puertos(
        identidad=escenario_cotizacion.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        ejecutor=escenario.ejecutor,
        perfiles=escenario_cotizacion.perfiles,
        reloj=escenario_cotizacion.reloj,
        productos=escenario_cotizacion.productos,
        cotizaciones=escenario_cotizacion.repo,
    )


def test_con_puertos_el_task_id_llega_a_la_cotizacion_sin_salir_en_la_respuesta(
    puertos: Puertos,
) -> None:
    with respx.mock(assert_all_called=True) as metadatos:
        ruta = metadatos.get(f"{URI}/task").respond(200, json={"TaskARN": TASK_ARN})
        app = crear_app(Settings(**{VARIABLE: URI}), puertos=puertos)
        with TestClient(app) as cliente:
            assert cliente.app.state.calcular_prima.config.task_id == TASK_ID  # type: ignore[attr-defined]
            respuesta = cliente.post(
                "/v1/cotizaciones",
                json=C01,
                headers={**ASESOR, "Idempotency-Key": "00000000-0000-4000-c000-000000000071"},
            )
            assert respuesta.status_code == 201, respuesta.text
            assert TASK_ID not in respuesta.text
            assert TASK_ID not in json.dumps(dict(respuesta.headers))
        assert ruta.call_count == 1, "una sola lectura al arrancar"


def test_con_los_metadatos_en_un_puerto_cerrado_la_app_arranca_y_responde(
    puertos: Puertos,
) -> None:
    uri = f"http://127.0.0.1:{puerto_cerrado()}/v4/sintetico"
    inicio = time.monotonic()
    with TestClient(crear_app(Settings(**{VARIABLE: uri}), puertos=puertos)) as cliente:
        assert time.monotonic() - inicio < 1 + 2, "la lectura no debe demorar el arranque"
        assert cliente.app.state.calcular_prima.config.task_id is None  # type: ignore[attr-defined]
        assert cliente.get("/health").json() == {"status": "ok"}
        respuesta = cliente.post(
            "/v1/cotizaciones",
            json=C01,
            headers={**ASESOR, "Idempotency-Key": "00000000-0000-4000-c000-000000000072"},
        )
        assert respuesta.status_code == 201, respuesta.text


def test_sin_variable_no_hay_task_id(puertos: Puertos, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VARIABLE, raising=False)
    with respx.mock(assert_all_mocked=True) as metadatos:
        with TestClient(crear_app(Settings(), puertos=puertos)) as cliente:
            assert cliente.app.state.calcular_prima.config.task_id is None  # type: ignore[attr-defined]
        assert metadatos.calls.call_count == 0


def test_en_la_rama_con_base_tambien_se_lee(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg_pool
    from cotizacion.adapters.outbound import perfiles_pg

    class PoolFalso:
        def __init__(self, url: str, **_: Any) -> None:
            self.url = url

        async def open(self) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(perfiles_pg, "migrar", lambda _url: None)
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)
    with respx.mock(assert_all_called=True) as metadatos:
        metadatos.get(f"{URI}/task").respond(200, json={"TaskARN": TASK_ARN})
        settings = Settings(**{VARIABLE: URI, "DB_HOST": "rds.interno"})
        with TestClient(crear_app(settings)) as cliente:
            assert cliente.app.state.calcular_prima.config.task_id == TASK_ID  # type: ignore[attr-defined]
