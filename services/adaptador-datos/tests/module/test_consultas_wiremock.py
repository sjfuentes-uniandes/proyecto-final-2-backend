"""MT-W10-08 a 16: adaptador-datos contra WireMock con los mapeos de C01–C19 (03, W10-P08)."""

import time
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("wiremock")

CONSENTIMIENTO = "00000000-0000-4000-9000-000000000001"
CORRELACION = "corr-modulo-w10"
CAMPOS = {
    "FA_PRODUCTOS_VIGENTES": [
        "antiguedad_productos_anios",
        "cuota_mensual_obligaciones",
        "entidades_con_deuda",
    ],
    "FA_HISTORIAL_PAGOS_12M": ["moras_12m"],
    "FA_INGRESOS_AGREGADOS": ["ingreso_mensual_estimado"],
    "DA_REGISTROS_PUBLICOS": ["coincidencias_listas_restrictivas"],
}


def _cliente(nn: str) -> str:
    return f"00000000-0000-4000-8000-0000000000{nn}"


def _consultar(
    adaptador: Any, nn: str, fuente: str, headers: dict[str, str] | None = None, **cambios: Any
) -> Any:
    cuerpo: dict[str, Any] = {
        "fuente": fuente,
        "clienteId": _cliente(nn),
        "consentimientoId": CONSENTIMIENTO,
        "proposito": "PERFILAMIENTO_PRECIO",
        "camposAutorizados": CAMPOS[fuente],
    }
    cuerpo.update(cambios)
    return adaptador.cliente.post(
        "/v1/consultas",
        json=cuerpo,
        headers={"X-Correlation-Id": CORRELACION, **(headers or {})},
    )


def _valores(respuesta: Any) -> dict[str, Any]:
    return {c: v["valor"] for c, v in respuesta.json()["campos"].items()}


def test_mt_w10_08_c01_las_cuatro_fuentes_nominal(app_adaptador: Any) -> None:
    adaptador = app_adaptador()
    esperados = {
        "FA_PRODUCTOS_VIGENTES": (
            {
                "antiguedad_productos_anios": "7",
                "cuota_mensual_obligaciones": "1800000.00",
                "entidades_con_deuda": "2",
            },
            0.95,
            "3.1",
        ),
        "FA_HISTORIAL_PAGOS_12M": ({"moras_12m": "0"}, 0.91, "3.1"),
        "FA_INGRESOS_AGREGADOS": ({"ingreso_mensual_estimado": "6500000.00"}, 0.93, "3.1"),
        "DA_REGISTROS_PUBLICOS": ({"coincidencias_listas_restrictivas": "0"}, 0.88, "1.8"),
    }
    for fuente, (valores, confianza, version) in esperados.items():
        respuesta = _consultar(adaptador, "01", fuente)
        assert respuesta.status_code == 200, respuesta.text
        cuerpo = respuesta.json()
        assert _valores(respuesta) == valores
        assert (cuerpo["confianza"], cuerpo["versionFuente"]) == (confianza, version)
        assert cuerpo["camposDescartados"] == 0
        assert cuerpo["consultadaEn"].endswith("Z")


def test_mt_w10_09_journal_con_campos_exactos_y_correlacion(
    app_adaptador: Any, wiremock: Any, leer_encabezado: Any
) -> None:
    adaptador = app_adaptador()
    assert _consultar(adaptador, "01", "FA_PRODUCTOS_VIGENTES").status_code == 200
    (solicitud,) = wiremock.de_cliente(_cliente("01"))
    assert solicitud["queryParams"]["fields"]["values"] == [
        "creditorCount,oldestProductYears,totalMonthlyInstallment"
    ]
    assert set(solicitud["queryParams"]) == {"fields"}
    assert leer_encabezado(solicitud, "X-Correlation-Id") == CORRELACION
    assert leer_encabezado(solicitud, "X-Api-Key") == "local-sintetico"


def test_mt_w10_10_c04_campos_adicionales_filtrados(app_adaptador: Any) -> None:
    adaptador = app_adaptador()
    respuesta = _consultar(adaptador, "04", "FA_PRODUCTOS_VIGENTES")
    assert respuesta.status_code == 200
    assert respuesta.json()["camposDescartados"] == 3
    assert set(respuesta.json()["campos"]) == set(CAMPOS["FA_PRODUCTOS_VIGENTES"])
    for centinela in ("SIN-CTA-0004", "Cliente Sintético 04", "accountNumbers", "fullName"):
        assert centinela not in respuesta.text
        assert centinela not in adaptador.logs.getvalue()
        assert centinela not in adaptador.texto_spans()


def test_mt_w10_11_c03_dato_ausente_no_disponible(app_adaptador: Any) -> None:
    respuesta = _consultar(app_adaptador(), "03", "FA_HISTORIAL_PAGOS_12M")
    assert respuesta.status_code == 200
    assert respuesta.json()["campos"] == {"moras_12m": {"estado": "NO_DISPONIBLE", "valor": None}}


def test_mt_w10_12_c05_respuesta_malformada(app_adaptador: Any) -> None:
    respuesta = _consultar(app_adaptador(), "05", "FA_PRODUCTOS_VIGENTES")
    assert respuesta.status_code == 502
    assert respuesta.json()["codigo"] == "RESPUESTA_INVALIDA"
    assert respuesta.json()["correlationId"] == CORRELACION


def test_mt_w10_13_c07_proveedor_caido(app_adaptador: Any) -> None:
    respuesta = _consultar(app_adaptador(), "07", "FA_INGRESOS_AGREGADOS")
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "FUENTE_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {"motivo": "ERROR", "circuito": "CERRADO"}


def test_mt_w10_14_c06_lento_corta_por_deadline(app_adaptador: Any) -> None:
    adaptador = app_adaptador()
    inicio = time.perf_counter()
    respuesta = _consultar(adaptador, "06", "DA_REGISTROS_PUBLICOS", {"X-Deadline-Ms": "290"})
    duracion_ms = (time.perf_counter() - inicio) * 1000
    assert respuesta.status_code == 504
    assert respuesta.json()["codigo"] == "TIEMPO_AGOTADO"
    assert duracion_ms <= 340, duracion_ms


def test_mt_w10_15_sin_consentimiento_no_llama_al_proveedor(
    app_adaptador: Any, wiremock: Any
) -> None:
    respuesta = _consultar(app_adaptador(), "01", "FA_HISTORIAL_PAGOS_12M", consentimientoId=None)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "CONSENTIMIENTO_REQUERIDO"
    assert wiremock.solicitudes() == []


def test_mt_w10_16_campo_no_soportado(app_adaptador: Any, wiremock: Any) -> None:
    respuesta = _consultar(
        app_adaptador(),
        "01",
        "FA_HISTORIAL_PAGOS_12M",
        camposAutorizados=["ingreso_mensual_estimado"],
    )
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "CAMPOS_NO_SOPORTADOS"
    assert wiremock.solicitudes() == []


def test_zz_centinelas_ausentes_en_logs_y_spans(registro: Any, centinelas: tuple[str, ...]) -> None:
    """Corre al final de la suite: ningún valor sensible quedó en logs ni trazas (01 §3.9)."""
    assert registro.logs, "la suite no capturó logs"
    nombres = {s.name for e in registro.spans for s in e.get_finished_spans()}
    assert "consulta.FA_PRODUCTOS_VIGENTES" in nombres  # span de la operación
    assert any(n.startswith("GET") for n in nombres), nombres  # span cliente HTTPX al aliado
    texto = registro.texto()
    assert "consulta.FA_PRODUCTOS_VIGENTES" in texto
    for centinela in centinelas:
        assert centinela not in texto, centinela
