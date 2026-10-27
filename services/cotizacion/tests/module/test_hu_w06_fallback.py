"""N2 HU-W06: respuesta degradada con PostgreSQL efímero, catalogo real (reglas de fallback
2026.10.1 de su semilla), adaptador-datos real y el simulador de aliados (WireMock).

Por fuente, con los modos del simulador:
- C07 `caido` en FA_INGRESOS_AGREGADOS (opcional, FB-003 DEGRADAR) → oferta degradada (AC1, AC3).
- `malformada` en FA_INGRESOS_AGREGADOS (mapeo sobrescrito solo en la prueba) → degradada con
  RESPUESTA_INVALIDA; ningún campo parcial.
- C06 `lento` en DA_REGISTROS_PUBLICOS (opcional, FB-004 DEGRADAR) → degradada con TIEMPO_AGOTADO
  (el recorrido del N3 de HU-W05 sigue dando oferta).
- C05 `malformada` en FA_PRODUCTOS_VIGENTES y C13 `lento` en FA_HISTORIAL_PAGOS_12M (obligatorias)
  → 503 sin oferta, decisión PENDIENTE (AC2).
- Varias fuentes a la vez: las dos opcionales caídas → degradada con las dos; una opcional y una
  obligatoria → PENDIENTE con las dos.
En todos: la decisión en `decisiones_fallback` y el evento CotizacionDegradada en el outbox, válido
contra contracts/events y sin datos personales ni financieros.
"""

import json
from datetime import UTC
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from .conftest import ACTOR, CASOS, DECLARADOS_C01, EVENTOS, cliente_id, nn_de

pytest.importorskip("docker")

PRESUPUESTO = {"fuente_timeout_ms": 300}
INGRESOS_C07 = "/open-finance/v3/customers/00000000-0000-4000-8000-000000000007/aggregated-income"
REGISTROS_C07 = "/datos-abiertos/v1/registros/00000000-0000-4000-8000-000000000007"
HISTORIAL_C07 = "/open-finance/v3/customers/00000000-0000-4000-8000-000000000007/payment-history"
# Valores de las fuentes (C01–C27) y declarados: nunca en una respuesta degradada ni en un evento.
SENSIBLES = ("6500000", "1800000.00", "1990-04-18", "Cliente Prueba")
ESQUEMA = Draft202012Validator(
    json.loads((EVENTOS / "CotizacionDegradada.v1.schema.json").read_text(encoding="utf-8"))
)


def _mapeo(url: str, consulta: dict[str, Any], respuesta: dict[str, Any]) -> dict[str, Any]:
    return {
        "request": {
            "method": "GET",
            "urlPath": url,
            "queryParameters": consulta,
            "headers": {"X-Api-Key": {"matches": ".+"}},
        },
        "response": {"headers": {"Content-Type": "application/json"}, **respuesta},
    }


INGRESOS_MALFORMADA = _mapeo(
    INGRESOS_C07,
    {"fields": {"equalTo": "estimatedMonthlyIncome"}},
    {
        "status": 200,
        "jsonBody": {"schemaVersion": "3.1", "confidenceScore": 0.93, "data": "no-es-objeto"},
    },
)
REGISTROS_CAIDO = _mapeo(
    REGISTROS_C07,
    {"campos": {"equalTo": "listasRestrictivas"}},
    {"status": 503, "jsonBody": {"error": "unavailable"}},
)
HISTORIAL_CAIDO = _mapeo(
    HISTORIAL_C07,
    {"months": {"equalTo": "12"}, "fields": {"equalTo": "lateCount"}},
    {"status": 503, "jsonBody": {"error": "unavailable"}},
)


def _perfil(svc: Any, nn: str) -> dict[str, Any]:
    respuesta = svc.enviar(nn)
    assert respuesta.status_code == 201, respuesta.text
    perfil: dict[str, Any] = respuesta.json()
    return perfil


def _cotizar(svc: Any, nn: str, perfil_id: str) -> Any:
    cuerpo = {
        "clienteId": cliente_id(nn),
        "productoId": "vida-hipotecario",
        "datosAsegurado": DECLARADOS_C01,
        "perfilId": perfil_id,
    }
    return svc.cliente.post(
        "/v1/cotizaciones", json=cuerpo, headers={**ACTOR, "Idempotency-Key": str(uuid4())}
    )


def _decisiones(svc: Any, perfil_id: str) -> list[tuple[Any, ...]]:
    return svc.bases.filas(
        "SELECT id, cotizacion_id, tipo, faltantes, version_reglas FROM decisiones_fallback "
        "WHERE perfil_id = %s",
        perfil_id,
    )


def _evento(svc: Any, decision_id: Any) -> dict[str, Any]:
    """El sobre CotizacionDegradada del outbox, validado contra su esquema publicado."""
    filas = svc.bases.filas(
        "SELECT event_id, aggregate_id, event_type, event_version, occurred_at, correlation_id, "
        "payload, published_at FROM outbox WHERE event_type = 'CotizacionDegradada' "
        "AND payload->>'decisionId' = %s",
        str(decision_id),
    )
    assert len(filas) == 1, filas
    event_id, agregado, tipo, version, ocurrido, correlacion, payload, publicado = filas[0]
    sobre = {
        "eventId": str(event_id),
        "aggregateId": str(agregado),
        "type": tipo,
        "version": version,
        "occurredAt": ocurrido.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "correlationId": correlacion,
        "payload": payload,
    }
    ESQUEMA.validate(sobre)
    assert publicado is None  # Pendiente del relay del outbox.
    texto = json.dumps(sobre)
    for sensible in SENSIBLES:
        assert sensible not in texto
    assert "clienteId" not in payload
    return sobre


def _sin_sensibles(cuerpo: Any) -> None:
    texto = json.dumps(cuerpo)
    for sensible in ("6500000", "1800000.00"):
        assert sensible not in texto


# --- AC1 y AC3: fuente opcional con regla DEGRADAR ---------------------------------------------


def test_n2_c07_opcional_caida_da_oferta_degradada_con_su_decision(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    perfil = _perfil(svc, "07")
    assert perfil["estado"] == "DEGRADADO"
    respuesta = _cotizar(svc, "07", perfil["id"])
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    faltante = {
        "fuente": "FA_INGRESOS_AGREGADOS",
        "dato": "ingreso_mensual_estimado",
        "motivo": "NO_DISPONIBLE",
        "regla": {"id": "FB-003", "version": "2026.10.1"},
    }
    assert (cuerpo["degradada"], cuerpo["faltantes"]) == (True, [faltante])
    assert cuerpo["oferta"]["codigo"].startswith("COT-")
    assert cuerpo["entradas"]["nivelRiesgo"] == perfil["nivelRiesgo"]
    _sin_sensibles(cuerpo)
    # AC3: la decisión se consulta y queda persistida con la cotización.
    decision = svc.cliente.get(f"/v1/cotizaciones/{cuerpo['id']}/decision", headers=ACTOR).json()
    assert (decision["decision"], decision["faltantes"]) == ("DEGRADADA", [faltante])
    [(decision_id, cotizacion_id, tipo, faltantes, version)] = _decisiones(svc, perfil["id"])
    assert (str(cotizacion_id), tipo, version) == (cuerpo["id"], "DEGRADADA", "2026.10.1")
    assert str(decision_id) == decision["decisionId"]
    assert faltantes[0]["regla"]["accion"] == "DEGRADAR"
    sobre = _evento(svc, decision_id)
    print(f"\nCotizacionDegradada en el outbox: {json.dumps(sobre, ensure_ascii=False)}")
    assert sobre["aggregateId"] == cuerpo["id"]
    assert sobre["payload"]["faltantes"][0]["motivo"] == "NO_DISPONIBLE"
    # Telemetría: la cotización queda `degradado`; el proveedor caído, `error` en su consulta.
    cierre = [json.loads(l) for l in svc.logs.getvalue().splitlines() if l.strip()]
    resultado = [
        l["result"] for l in cierre if l.get("operation") == "cotizacion" and "result" in l
    ]
    assert resultado[-1] == "degradado"
    assert [s for s in svc.wiremock.a_aliados(cliente_id("07")) if "aggregated-income" in s["url"]]


def test_n2_opcional_malformada_tambien_degrada_sin_campos_parciales(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    with svc.wiremock.sobrescribir(INGRESOS_MALFORMADA):
        perfil = _perfil(svc, "07")
    fuente = next(f for f in perfil["fuentes"] if f["fuente"] == "FA_INGRESOS_AGREGADOS")
    assert fuente["estado"] == "RESPUESTA_INVALIDA"
    assert not fuente.get("camposDisponibles")
    linaje = svc.bases.filas(
        "SELECT estado, campos_disponibles FROM linaje_fuentes "
        "WHERE perfil_id = %s AND fuente = %s",
        perfil["id"],
        "FA_INGRESOS_AGREGADOS",
    )
    assert linaje == [("RESPUESTA_INVALIDA", [])]
    # Ningún campo de la respuesta malformada entró a los insumos del perfil.
    [(insumos,)] = svc.bases.filas(
        "SELECT insumos FROM perfiles_riesgo WHERE id = %s", perfil["id"]
    )
    assert "ingreso_mensual_estimado" not in insumos
    cuerpo = _cotizar(svc, "07", perfil["id"]).json()
    assert cuerpo["degradada"] is True
    assert cuerpo["faltantes"][0]["motivo"] == "RESPUESTA_INVALIDA"
    [(decision_id, *_)] = _decisiones(svc, perfil["id"])
    assert _evento(svc, decision_id)["payload"]["decision"] == "DEGRADADA"


def test_n2_c06_opcional_lenta_degrada_y_el_recorrido_de_w05_sigue_con_oferta(
    cotizacion: Any,
) -> None:
    svc = cotizacion(**PRESUPUESTO)
    perfil = _perfil(svc, "06")
    respuesta = _cotizar(svc, "06", perfil["id"])
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["oferta"] is not None and cuerpo["degradada"] is True
    assert [(f["fuente"], f["motivo"], f["regla"]["id"]) for f in cuerpo["faltantes"]] == [
        ("DA_REGISTROS_PUBLICOS", "TIEMPO_AGOTADO", "FB-004")
    ]
    [(decision_id, *_)] = _decisiones(svc, perfil["id"])
    _evento(svc, decision_id)


def test_n2_las_dos_opcionales_caidas_a_la_vez_degradan_con_las_dos(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    with svc.wiremock.sobrescribir(REGISTROS_CAIDO):
        perfil = _perfil(svc, "07")
    cuerpo = _cotizar(svc, "07", perfil["id"]).json()
    assert cuerpo["degradada"] is True
    assert [(f["fuente"], f["regla"]["id"]) for f in cuerpo["faltantes"]] == [
        ("DA_REGISTROS_PUBLICOS", "FB-004"),
        ("FA_INGRESOS_AGREGADOS", "FB-003"),
    ]
    [(decision_id, *_)] = _decisiones(svc, perfil["id"])
    assert len(_evento(svc, decision_id)["payload"]["faltantes"]) == 2


# --- AC2: sin fallback permitido -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("nn", "fuente", "motivo", "regla"),
    [
        ("05", "FA_PRODUCTOS_VIGENTES", "RESPUESTA_INVALIDA", "FB-001"),
        ("0d", "FA_HISTORIAL_PAGOS_12M", "TIEMPO_AGOTADO", "FB-002"),
    ],
    ids=["C05 obligatoria malformada", "C13 obligatoria lenta"],
)
def test_n2_sin_fallback_permitido_es_pendiente_sin_oferta(
    cotizacion: Any, nn: str, fuente: str, motivo: str, regla: str
) -> None:
    svc = cotizacion(**PRESUPUESTO)
    perfil = _perfil(svc, nn)
    antes = svc.bases.filas("SELECT count(*) FROM cotizaciones")[0][0]
    respuesta = _cotizar(svc, nn, perfil["id"])
    assert respuesta.status_code == 503, respuesta.text
    error = respuesta.json()
    assert error["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    detalles = error["detalles"]
    assert (detalles["dependencia"], detalles["accionSugerida"]) == (
        "perfil.fuentes",
        "RECALCULAR_PERFIL",
    )
    assert {(f["fuente"], f["motivo"], f["regla"]["id"]) for f in detalles["faltantes"]} == {
        (fuente, motivo, regla)
    }
    _sin_sensibles(error)
    # Sin oferta definitiva: ninguna cotización nueva; la decisión y su evento, sí.
    assert svc.bases.filas("SELECT count(*) FROM cotizaciones")[0][0] == antes
    [(decision_id, cotizacion_id, tipo, _, _)] = _decisiones(svc, perfil["id"])
    assert (str(decision_id), cotizacion_id, tipo) == (detalles["decisionId"], None, "PENDIENTE")
    sobre = _evento(svc, decision_id)
    assert sobre["aggregateId"] == str(decision_id)
    assert sobre["payload"]["accionSugerida"] == "RECALCULAR_PERFIL"


def test_n2_una_opcional_y_una_obligatoria_caidas_quedan_pendientes(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    with svc.wiremock.sobrescribir(HISTORIAL_CAIDO):
        perfil = _perfil(svc, "07")
    assert perfil["estado"] == "INFORMACION_INSUFICIENTE"
    respuesta = _cotizar(svc, "07", perfil["id"])
    assert respuesta.status_code == 503
    faltantes = respuesta.json()["detalles"]["faltantes"]
    assert [(f["fuente"], f["motivo"], f["regla"]["id"]) for f in faltantes] == [
        ("FA_HISTORIAL_PAGOS_12M", "NO_DISPONIBLE", "FB-002"),
        ("FA_INGRESOS_AGREGADOS", "NO_DISPONIBLE", "FB-003"),
    ]
    [(decision_id, *_)] = _decisiones(svc, perfil["id"])
    assert len(_evento(svc, decision_id)["payload"]["faltantes"]) == 2


# --- Recorrido nominal -------------------------------------------------------------------------


def test_n2_c01_nominal_no_degrada_ni_escribe_decision(cotizacion: Any) -> None:
    svc = cotizacion(**PRESUPUESTO)
    perfil = _perfil(svc, "01")
    cuerpo = _cotizar(svc, "01", perfil["id"]).json()
    assert (cuerpo["degradada"], cuerpo["faltantes"]) == (False, [])
    decision = svc.cliente.get(f"/v1/cotizaciones/{cuerpo['id']}/decision", headers=ACTOR).json()
    assert decision["decision"] == "NOMINAL"
    assert _decisiones(svc, perfil["id"]) == []


# --- Casos sintéticos C20–C27 (contracts/datos-sinteticos), de punta a punta -------------------

CASOS_W06 = [c for c in CASOS if "cotizacion" in c["esperado"]]


@pytest.mark.parametrize("caso", CASOS_W06, ids=lambda c: c["id"])
def test_n2_cada_caso_sintetico_de_hu_w06_produce_su_resultado(
    cotizacion: Any, caso: dict[str, Any]
) -> None:
    """La semilla de clientes y los mapeos del simulador reproducen el perfil y la decisión de
    fallback que declara cada caso (los que se usan para probar HU-W06 a mano)."""
    svc = cotizacion(**PRESUPUESTO)
    nn = nn_de(caso["clienteId"])
    esperado = caso["esperado"]
    perfil = _perfil(svc, nn)
    assert perfil["estado"] == esperado["estado"]
    assert perfil["nivelRiesgo"] == esperado["nivel"]
    assert perfil["puntaje"] == esperado["puntaje"]
    estados = {f["fuente"]: f["estado"] for f in perfil["fuentes"]}
    assert {f: estados[f] for f in esperado["fuentes"]} == esperado["fuentes"]
    respuesta = _cotizar(svc, nn, perfil["id"])
    cotizacion_esperada = esperado["cotizacion"]
    assert respuesta.status_code == cotizacion_esperada["http"], respuesta.text
    cuerpo = respuesta.json()
    detalles = cuerpo if respuesta.status_code == 201 else cuerpo["detalles"]
    reglas = list(dict.fromkeys(f["regla"]["id"] for f in detalles["faltantes"]))
    assert reglas == cotizacion_esperada["reglasFallback"]
    [(_, _, tipo, _, _)] = _decisiones(svc, perfil["id"])
    assert tipo == cotizacion_esperada["decision"]
