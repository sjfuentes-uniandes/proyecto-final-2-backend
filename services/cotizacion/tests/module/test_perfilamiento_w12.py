"""Pruebas de módulo de HU-W12 (MT-W12-05 a 12): consulta en paralelo, timeout por rama,
recálculo y telemetría, con PostgreSQL, WireMock y las apps reales (arnés de W11-P14).

Todas usan el presupuesto por defecto de DT-10 (`fuente_timeout_ms = 300`): el adaptador
recibe `X-Deadline-Ms = 290` y corta al aliado en 270 ms.
"""

import json
import time
from typing import Any
from uuid import uuid4

from cotizacion.domain.perfilamiento.modelos import Fuente

FUENTES = [f.value for f in Fuente]
RAMAS = {f"perfilamiento.dependencia.fuente.{f}" for f in FUENTES}
PRESUPUESTO = {"fuente_timeout_ms": 300}


def cliente_id(nn: str) -> str:
    return f"00000000-0000-4000-8000-0000000000{nn}"


def _estados(perfil: dict[str, Any]) -> dict[str, str]:
    return {f["fuente"]: f["estado"] for f in perfil["fuentes"]}


def _linaje(svc: Any, perfil_id: str) -> list[tuple[Any, ...]]:
    return svc.bases.filas(
        "SELECT fuente, estado, duracion_ms FROM linaje_fuentes WHERE perfil_id = %s ORDER BY orden",
        perfil_id,
    )


def _logs(svc: Any) -> list[dict[str, Any]]:
    return [json.loads(linea) for linea in svc.logs.getvalue().splitlines() if linea.strip()]


def _clasificacion(perfil: dict[str, Any]) -> tuple[Any, ...]:
    return (perfil["estado"], perfil["nivelRiesgo"], perfil["puntaje"], perfil["confianza"])


def test_mt_w12_05_c18_las_cuatro_ramas_en_paralelo(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    respuesta = svc.enviar("12")
    assert respuesta.status_code == 201, respuesta.text
    perfil = respuesta.json()
    assert (perfil["estado"], perfil["puntaje"]) == ("CALCULADO", 75.9)
    assert perfil["duracionMs"] < 350, perfil["duracionMs"]  # en serie serían ≥ 600
    solicitudes = svc.wiremock.a_aliados(cliente_id("12"))
    assert len(solicitudes) == 4
    llegadas = [s["loggedDate"] for s in solicitudes]
    assert max(llegadas) - min(llegadas) < 100, llegadas


def test_mt_w12_06_c06_fuente_lenta_degradada(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    respuesta = svc.enviar("06")
    assert respuesta.status_code == 201, respuesta.text
    perfil = respuesta.json()
    assert _clasificacion(perfil) == ("DEGRADADO", "BAJO", 74.09, 0.79)
    assert perfil["duracionMs"] < 450, perfil["duracionMs"]
    assert _estados(perfil)["DA_REGISTROS_PUBLICOS"] == "TIEMPO_AGOTADO"
    linaje = {fuente: estado for fuente, estado, _ in _linaje(svc, perfil["id"])}
    assert linaje["DA_REGISTROS_PUBLICOS"] == "TIEMPO_AGOTADO"
    rama = [
        linea
        for linea in _logs(svc)
        if linea.get("operation") == "perfilamiento.dependencia.fuente.DA_REGISTROS_PUBLICOS"
    ]
    assert [linea["result"] for linea in rama] == ["timeout"]
    (cierre,) = [linea for linea in _logs(svc) if linea.get("message") == "perfil calculado"]
    dependencias = {d["nombre"]: d["resultado"] for d in cierre["dependencias"]}
    assert dependencias["fuente.DA_REGISTROS_PUBLICOS"] == "timeout"


def test_mt_w12_07_c13_fuente_obligatoria_lenta(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    respuesta = svc.enviar("0d")
    assert respuesta.status_code == 201, respuesta.text
    perfil = respuesta.json()
    assert perfil["estado"] == "INFORMACION_INSUFICIENTE"
    assert {
        "campo": "moras_12m",
        "fuente": "FA_HISTORIAL_PAGOS_12M",
        "motivo": "TIEMPO_AGOTADO",
    } in (perfil["datosRequeridos"])
    assert perfil["accionSugerida"] == "RECALCULAR"
    assert _estados(perfil)["FA_HISTORIAL_PAGOS_12M"] == "TIEMPO_AGOTADO"


def test_mt_w12_08_c07_fuente_caida(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    respuesta = svc.enviar("07")
    assert respuesta.status_code == 201, respuesta.text
    perfil = respuesta.json()
    assert _clasificacion(perfil) == ("DEGRADADO", "BAJO", 75.0, 0.78)
    assert _estados(perfil)["FA_INGRESOS_AGREGADOS"] == "NO_DISPONIBLE"
    ingresos = [
        s for s in svc.wiremock.a_aliados(cliente_id("07")) if "aggregated-income" in s["url"]
    ]
    assert 1 <= len(ingresos) <= 2  # un reintento acotado ante 503


def test_mt_w12_09_trazas_por_rama_con_el_mismo_correlation_id(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    respuesta = svc.enviar("01", correlation_id="corr-mt-w12-09")
    assert respuesta.status_code == 201, respuesta.text
    spans = [s for s in svc.lista_spans() if s.attributes.get("correlation_id") == "corr-mt-w12-09"]
    (padre,) = [s for s in spans if s.name == "perfilamiento"]
    ramas = [s for s in spans if s.name in RAMAS]
    assert {s.name for s in ramas} == RAMAS and len(ramas) == 4
    for rama in ramas:
        assert rama.parent is not None and rama.parent.span_id == padre.context.span_id
        assert rama.context.trace_id == padre.context.trace_id
        assert rama.attributes["result"] == "ok"
    solicitudes = svc.wiremock.a_aliados(cliente_id("01"))
    assert len(solicitudes) == 4
    for solicitud in solicitudes:
        encabezados = {k.lower(): v for k, v in solicitud["headers"].items()}
        assert encabezados["x-correlation-id"] == "corr-mt-w12-09"


def test_mt_w12_10_metricas_y_duracion_por_dependencia(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    respuesta = svc.enviar("01")
    assert respuesta.status_code == 201, respuesta.text
    datos = svc.lector.get_metrics_data()
    operaciones = {
        punto.attributes["Operation"]
        for recurso in datos.resource_metrics
        for alcance in recurso.scope_metrics
        for metrica in alcance.metrics
        if metrica.name == "OperationDuration"
        for punto in metrica.data.data_points
    }
    assert {"perfilamiento", *RAMAS} <= operaciones
    linaje = _linaje(svc, respuesta.json()["id"])
    assert [fuente for fuente, _, _ in linaje] == FUENTES
    assert all(ms is not None for _, estado, ms in linaje if estado == "CONSULTADA")
    assert all(f["duracionMs"] is not None for f in respuesta.json()["fuentes"])


def test_mt_w12_11_c19_recuperacion_con_recalculo(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    svc.wiremock.reiniciar_escenarios()
    primero = svc.enviar("13")
    assert primero.status_code == 201, primero.text
    anterior = primero.json()
    assert anterior["estado"] == "DEGRADADO"
    assert _estados(anterior)["DA_REGISTROS_PUBLICOS"] == "TIEMPO_AGOTADO"

    recalculo = svc.cliente.post(
        f"/v1/perfiles/{anterior['id']}/recalculos",
        headers={
            "X-Actor-Id": "asesor-sintetico",
            "X-Actor-Rol": "ASESOR",
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert recalculo.status_code == 201, recalculo.text
    nuevo = recalculo.json()
    assert (nuevo["estado"], nuevo["puntaje"]) == ("CALCULADO", 75.9)
    assert nuevo["perfilAnteriorId"] == anterior["id"] and nuevo["id"] != anterior["id"]

    consultado = svc.cliente.get(
        f"/v1/perfiles/{anterior['id']}",
        headers={"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"},
    )
    assert consultado.json() == anterior  # DT-25: el anterior no cambia
    operaciones = {s.name for s in svc.lista_spans()}
    assert {"perfilamiento", "perfilamiento.recalculo"} <= operaciones


def test_mt_w12_12_adaptador_detenido(cotizacion: Any, wiremock: Any, servir_app: Any) -> None:
    from adaptador_datos.config import Settings as SettingsAdaptador
    from adaptador_datos.main import crear_app as crear_adaptador

    # Una app propia del adaptador, que se detiene antes del perfilamiento.
    settings = SettingsAdaptador(
        ALLY_OPEN_FINANCE_URL=f"{wiremock.url}/open-finance",
        ALLY_DATOS_ABIERTOS_URL=f"{wiremock.url}/datos-abiertos",
    )
    with servir_app(crear_adaptador(settings)) as url:
        pass
    svc = cotizacion(ADAPTADOR_DATOS_URL=url, **PRESUPUESTO)
    inicio = time.perf_counter()
    respuesta = svc.enviar("01")
    duracion_ms = (time.perf_counter() - inicio) * 1000
    assert respuesta.status_code == 201, respuesta.text
    perfil = respuesta.json()
    assert perfil["estado"] == "INFORMACION_INSUFICIENTE"
    assert set(_estados(perfil).values()) == {"NO_DISPONIBLE"}
    assert duracion_ms < 450, duracion_ms
    assert wiremock.a_aliados(cliente_id("01")) == []
