"""GET /v1/cotizaciones/{cotizacionId}/recaudo con los dobles (HU-W16, 16.13): estados, cuerpo,
actores y encabezados, como GET …/aceptacion."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from polizas.config import Settings
from polizas.domain.recaudo.modelos import ResultadoRecaudo, SituacionRecaudo
from polizas.domain.suscripcion.insumos import texto
from polizas.main import crear_app

ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
SUSCRIPTOR = {"X-Actor-Id": "sub-suscriptor-1", "X-Actor-Rol": "SUSCRIPTOR"}
TODAS = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": False,
}
RESUELTO_EN = datetime(2026, 10, 24, 15, 0, 1, 250000, tzinfo=UTC)


def _ruta(cotizacion_id: Any) -> str:
    return f"/v1/cotizaciones/{cotizacion_id}/recaudo"


def _aceptada(api: TestClient, sinteticos: Any, id_caso: str = "S01") -> dict[str, Any]:
    decision = api.post(
        "/v1/suscripciones",
        json={"cotizacionId": sinteticos.cotizacion_id(id_caso)},
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    ).json()
    aceptacion = api.post(
        f"/v1/cotizaciones/{decision['cotizacionId']}/aceptacion",
        json={
            "decisionId": decision["id"],
            "versionDeclaraciones": "2026.10.1",
            "declaraciones": TODAS,
        },
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert aceptacion.status_code == 201, aceptacion.json()
    cuerpo: dict[str, Any] = aceptacion.json()
    return cuerpo


def _proyectar(dobles: Any, clave: str, **campos: Any) -> None:
    resultado = ResultadoRecaudo(
        clave_recaudo=UUID(clave),
        recaudo_id=uuid4(),
        **{
            "estado": SituacionRecaudo.CONFIRMADO,
            "monto": Decimal("64638.10"),
            "moneda": "COP",
            "transaccion_id": "TX-SINTETICA-0001",
            "motivo": None,
            "ocurrido_en": RESUELTO_EN,
            **campos,
        },
    )
    dobles.recaudos.resultados[resultado.clave_recaudo] = resultado


def test_sin_aceptacion_404(api: TestClient) -> None:
    respuesta = api.get(_ruta(uuid4()), headers={**ASESOR, "X-Correlation-Id": "corr-recaudo-404"})
    assert respuesta.status_code == 404
    assert respuesta.json() == {
        "codigo": "ACEPTACION_NO_ENCONTRADA",
        "mensaje": "La aceptación no existe",
        "correlationId": "corr-recaudo-404",
        "detalles": {},
    }


def test_con_aceptacion_y_sin_evento_esta_pendiente_con_la_prima_de_la_instantanea(
    api: TestClient, sinteticos: Any
) -> None:
    aceptacion = _aceptada(api, sinteticos)
    insumos = sinteticos.insumos("S01")
    respuesta = api.get(_ruta(aceptacion["cotizacionId"]), headers=ASESOR)
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "estado": "PENDIENTE",
        "claveRecaudo": aceptacion["id"],
        "monto": texto(insumos.prima_total),
        "moneda": insumos.moneda,
        "transaccionId": None,
        "motivo": None,
        "actualizadoEn": aceptacion["aceptadaEn"],
    }


def test_confirmado_con_su_monto_y_transaccion(
    api: TestClient, sinteticos: Any, dobles: Any
) -> None:
    aceptacion = _aceptada(api, sinteticos)
    # 64638.10 se conserva como texto con su escala: no pasa por float (sería 64638.1).
    _proyectar(dobles, aceptacion["id"])
    respuesta = api.get(_ruta(aceptacion["cotizacionId"]), headers=ASESOR)
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "estado": "CONFIRMADO",
        "claveRecaudo": aceptacion["id"],
        "monto": "64638.10",
        "moneda": "COP",
        "transaccionId": "TX-SINTETICA-0001",
        "motivo": None,
        "actualizadoEn": "2026-10-24T15:00:01.250000Z",
    }


def test_rechazado_con_motivo_y_la_prima_de_la_instantanea(
    api: TestClient, sinteticos: Any, dobles: Any
) -> None:
    # RecaudoRechazado v1 no trae el monto: sale de la instantánea de la decisión.
    aceptacion = _aceptada(api, sinteticos)
    _proyectar(
        dobles,
        aceptacion["id"],
        estado=SituacionRecaudo.RECHAZADO,
        monto=None,
        moneda=None,
        transaccion_id=None,
        motivo="FONDOS_INSUFICIENTES",
    )
    insumos = sinteticos.insumos("S01")
    respuesta = api.get(_ruta(aceptacion["cotizacionId"]), headers=ASESOR)
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "estado": "RECHAZADO",
        "claveRecaudo": aceptacion["id"],
        "monto": texto(insumos.prima_total),
        "moneda": insumos.moneda,
        "transaccionId": None,
        "motivo": "FONDOS_INSUFICIENTES",
        "actualizadoEn": "2026-10-24T15:00:01.250000Z",
    }


def test_el_estado_de_otra_aceptacion_no_se_mezcla(
    api: TestClient, sinteticos: Any, dobles: Any
) -> None:
    aceptacion = _aceptada(api, sinteticos)
    _proyectar(dobles, str(uuid4()))
    respuesta = api.get(_ruta(aceptacion["cotizacionId"]), headers=ASESOR)
    assert respuesta.json()["estado"] == "PENDIENTE"


def test_actores_y_encabezados(api: TestClient, sinteticos: Any) -> None:
    aceptacion = _aceptada(api, sinteticos)
    ruta = _ruta(aceptacion["cotizacionId"])
    # Los mismos roles que GET …/aceptacion: ASESOR y SUSCRIPTOR leen.
    suscriptor = api.get(ruta, headers=SUSCRIPTOR)
    assert suscriptor.status_code == 200
    otro = api.get(ruta, headers={"X-Actor-Id": "a", "X-Actor-Rol": "CLIENTE"})
    assert (otro.status_code, otro.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")
    sin_actor = api.get(ruta)
    assert (sin_actor.status_code, sin_actor.json()["codigo"]) == (401, "ACTOR_REQUERIDO")
    sin_rol = api.get(ruta, headers={"X-Actor-Id": "a"})
    assert sin_rol.status_code == 401
    invalida = api.get(_ruta("no-es-uuid"), headers=ASESOR)
    assert (invalida.status_code, invalida.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    correlacion = api.get(ruta, headers={**ASESOR, "X-Correlation-Id": "corr-recaudo-1"})
    assert correlacion.headers["X-Correlation-Id"] == "corr-recaudo-1"


def test_sin_la_decision_responde_503(api: TestClient, sinteticos: Any, dobles: Any) -> None:
    # No pasa (llave e inmutabilidad); si pasara, 503 sin datos del caso, no 500.
    aceptacion = _aceptada(api, sinteticos)
    dobles.decisiones.decisiones.clear()
    respuesta = api.get(_ruta(aceptacion["cotizacionId"]), headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (
        503,
        "DEPENDENCIA_NO_DISPONIBLE",
    )


def test_sin_puertos_responde_503() -> None:
    with TestClient(crear_app(Settings())) as cliente:
        respuesta = cliente.get(_ruta(uuid4()), headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (
        503,
        "DEPENDENCIA_NO_DISPONIBLE",
    )


def test_puertos_sin_la_lectura_del_recaudo_responde_503(dobles: Any) -> None:
    # Arneses anteriores a la 16.13 (la R3 de dinero) construyen los puertos sin `recaudos`.
    from polizas.main import Puertos

    puertos = Puertos(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        dobles.aceptaciones,
    )
    with TestClient(crear_app(Settings(), puertos=puertos)) as cliente:
        respuesta = cliente.get(_ruta(uuid4()), headers=ASESOR)
        aceptacion = cliente.get(f"/v1/cotizaciones/{uuid4()}/aceptacion", headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (
        503,
        "DEPENDENCIA_NO_DISPONIBLE",
    )
    # El resto de la API sigue igual.
    assert aceptacion.status_code == 404
