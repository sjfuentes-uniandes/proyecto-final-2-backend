"""N2 de HU-W05 contra WireMock 3.9.2 real con los mapeos del simulador de aliados.

- AC3 nominal (C01): responde a tiempo y se usa normalmente.
- AC1 lento (C06, 1000 ms): se abandona dentro del límite y queda el WARNING con la correlación;
  la traza (exportador en memoria) lleva presupuesto, restante y timeout.
- AC2 caído (C07, 503): reintenta una vez si queda presupuesto y nunca lo excede.
- Liberación de recursos: tras el corte no quedan tareas ni conexiones ocupadas.
"""

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from solventa_common.resiliencia import ProteccionDependencia, TiempoAgotado

pytestmark = pytest.mark.usefixtures("wiremock")

CONSENTIMIENTO = "00000000-0000-4000-9000-000000000001"
CORRELACION = "corr-modulo-w05"
# Tolerancia medida: salto local a WireMock y planificación del TestClient.
TOLERANCIA_MS = 80
CAMPOS = {
    "FA_INGRESOS_AGREGADOS": ["ingreso_mensual_estimado"],
    "DA_REGISTROS_PUBLICOS": ["coincidencias_listas_restrictivas"],
}


def _cliente(nn: str) -> str:
    return f"00000000-0000-4000-8000-0000000000{nn}"


def _consultar(adaptador: Any, nn: str, fuente: str, deadline_ms: int) -> tuple[Any, float]:
    inicio = time.perf_counter()
    respuesta = adaptador.cliente.post(
        "/v1/consultas",
        json={
            "fuente": fuente,
            "clienteId": _cliente(nn),
            "consentimientoId": CONSENTIMIENTO,
            "proposito": "PERFILAMIENTO_PRECIO",
            "camposAutorizados": CAMPOS[fuente],
        },
        headers={"X-Correlation-Id": CORRELACION, "X-Deadline-Ms": str(deadline_ms)},
    )
    return respuesta, (time.perf_counter() - inicio) * 1000


def _logs(adaptador: Any, mensaje: str) -> list[dict[str, Any]]:
    lineas = [json.loads(l) for l in adaptador.logs.getvalue().splitlines() if l.strip()]
    return [l for l in lineas if l["message"] == mensaje]


def _span(adaptador: Any, nombre: str) -> dict[str, Any]:
    (span,) = [s for s in adaptador.spans.get_finished_spans() if s.name == nombre]
    return dict(span.attributes or {})


def test_ac3_nominal_responde_a_tiempo(app_adaptador: Any, wiremock: Any) -> None:
    adaptador = app_adaptador()
    respuesta, duracion_ms = _consultar(adaptador, "01", "FA_INGRESOS_AGREGADOS", 300)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["campos"]["ingreso_mensual_estimado"]["estado"] == "DISPONIBLE"
    assert duracion_ms < 300, duracion_ms
    assert len(wiremock.de_cliente(_cliente("01"))) == 1
    assert _logs(adaptador, "dependencia agotó su tiempo") == []
    atributos = _span(adaptador, "consulta.FA_INGRESOS_AGREGADOS")
    assert atributos["result"] == "ok"
    assert atributos["solventa.timeout_ms"] == 280


def test_ac1_lento_se_abandona_dentro_del_limite_y_se_registra(
    app_adaptador: Any, wiremock: Any
) -> None:
    adaptador = app_adaptador()
    respuesta, duracion_ms = _consultar(adaptador, "06", "DA_REGISTROS_PUBLICOS", 300)
    assert respuesta.status_code == 504, respuesta.text
    assert respuesta.json()["codigo"] == "TIEMPO_AGOTADO"
    # timeout_efectivo_ms(800 de ally_timeouts_ms, 300) = 280 ms; el aliado tarda 1000 ms.
    assert 280 <= duracion_ms <= 280 + TOLERANCIA_MS, duracion_ms
    assert len(wiremock.de_cliente(_cliente("06"))) == 1  # un timeout no se reintenta

    (registro,) = _logs(adaptador, "dependencia agotó su tiempo")
    assert registro["level"] == "WARNING"
    assert registro["correlation_id"] == CORRELACION
    assert registro["proveedor"] == "DATOS_ABIERTOS"
    assert registro["operacion"] == "DA_REGISTROS_PUBLICOS"
    assert registro["timeout_ms"] == 280
    assert 0 <= registro["restante_ms"] <= 40
    assert registro["trace_id"] is not None

    atributos = _span(adaptador, "consulta.DA_REGISTROS_PUBLICOS")
    assert atributos["result"] == "timeout"
    assert atributos["correlation_id"] == CORRELACION
    assert atributos["solventa.presupuesto_ms"] == 300
    assert atributos["solventa.restante_ms"] == 300
    assert atributos["solventa.timeout_ms"] == 280


def test_ac2_caido_reintenta_una_vez_dentro_del_presupuesto(
    app_adaptador: Any, wiremock: Any
) -> None:
    adaptador = app_adaptador()
    respuesta, duracion_ms = _consultar(adaptador, "07", "FA_INGRESOS_AGREGADOS", 400)
    assert respuesta.status_code == 503, respuesta.text
    assert respuesta.json()["detalles"] == {"motivo": "ERROR", "circuito": "CERRADO"}
    assert len(wiremock.de_cliente(_cliente("07"))) == 2
    assert duracion_ms <= 400, duracion_ms


def test_ac2_caido_sin_presupuesto_para_reintentar(app_adaptador: Any, wiremock: Any) -> None:
    adaptador = app_adaptador()
    # 140 ms: tras el primer 503 (~50 ms) y la espera de 25 ms quedan < 100 ms.
    respuesta, duracion_ms = _consultar(adaptador, "07", "FA_INGRESOS_AGREGADOS", 140)
    assert respuesta.status_code == 503, respuesta.text
    assert len(wiremock.de_cliente(_cliente("07"))) == 1
    assert duracion_ms <= 140, duracion_ms


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_el_corte_libera_la_tarea_y_la_conexion(wiremock: Any, anyio_backend: str) -> None:
    url = f"{wiremock.url}/datos-abiertos/v1/registros/{_cliente('06')}"
    parametros = {"campos": "listasRestrictivas"}
    encabezados = {"X-Api-Key": "local-sintetico"}
    proteccion = ProteccionDependencia(5, 30, 10)
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=1)) as cliente:

        async def llamada(timeout_s: float) -> httpx.Response:
            return await cliente.get(url, params=parametros, headers=encabezados, timeout=timeout_s)

        antes = asyncio.all_tasks()
        inicio = time.perf_counter()
        with pytest.raises(TiempoAgotado):
            await proteccion.ejecutar("DA_REGISTROS_PUBLICOS", "DATOS_ABIERTOS", llamada, 200, None)
        assert (time.perf_counter() - inicio) * 1000 <= 200 + TOLERANCIA_MS
        assert asyncio.all_tasks() - antes == set()
        pool = cliente._transport._pool  # type: ignore[attr-defined]
        # La conexión de la solicitud abandonada se cerró: ninguna queda ocupada.
        assert all(c.is_idle() or c.is_closed() for c in pool.connections)
        # Con un pool de una sola conexión, la siguiente solicitud la obtiene sin esperar.
        nominal = await cliente.get(
            f"{wiremock.url}/datos-abiertos/v1/registros/{_cliente('01')}",
            params=parametros,
            headers=encabezados,
            timeout=1,
        )
        assert nominal.status_code == 200
        assert len(pool.connections) <= 1
