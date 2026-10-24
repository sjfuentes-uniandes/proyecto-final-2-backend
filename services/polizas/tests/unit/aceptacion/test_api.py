"""API de la aceptación con los dobles (HU-W15, W15-B05): estados, cuerpos B2 y actores."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from polizas.adapters.inbound.http.aceptaciones import error_aceptacion
from polizas.config import Settings
from polizas.main import crear_app

ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
SUSCRIPTOR = {"X-Actor-Id": "sub-suscriptor-1", "X-Actor-Rol": "SUSCRIPTOR"}
TODAS = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": False,
}


def _decidir(api: TestClient, sinteticos: Any, id_caso: str) -> dict[str, Any]:
    respuesta = api.post(
        "/v1/suscripciones",
        json={"cotizacionId": sinteticos.cotizacion_id(id_caso)},
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    decision: dict[str, Any] = respuesta.json()
    return decision


def _aceptar(
    api: TestClient,
    decision: dict[str, Any],
    clave: str | None = None,
    actor: dict[str, str] = ASESOR,
    **cambios: Any,
) -> Any:
    cuerpo = {
        "decisionId": decision["id"],
        "versionDeclaraciones": "2026.10.1",
        "declaraciones": TODAS,
        **cambios,
    }
    return api.post(
        f"/v1/cotizaciones/{decision['cotizacionId']}/aceptacion",
        json=cuerpo,
        headers={**actor, "Idempotency-Key": clave or str(uuid4())},
    )


def test_201_y_luego_200_con_la_misma_clave(api: TestClient, sinteticos: Any) -> None:
    decision = _decidir(api, sinteticos, "S01")
    clave = str(uuid4())
    creada = _aceptar(api, decision, clave)
    assert creada.status_code == 201, creada.json()
    cuerpo = creada.json()
    assert (cuerpo["recaudoHabilitado"], cuerpo["siguientePaso"], cuerpo["canal"]) == (
        True,
        "FIRMA",
        "PORTAL_ASESOR",
    )
    assert (cuerpo["cotizacionId"], cuerpo["decisionId"]) == (
        decision["cotizacionId"],
        decision["id"],
    )
    assert cuerpo["declaraciones"] == TODAS
    assert cuerpo["huellaEvidencia"].startswith("sha256:")
    repetida = _aceptar(api, decision, clave)
    assert (repetida.status_code, repetida.json()) == (200, cuerpo)
    consulta = api.get(f"/v1/cotizaciones/{decision['cotizacionId']}/aceptacion", headers=ASESOR)
    assert (consulta.status_code, consulta.json()) == (200, cuerpo)
    lectura_suscriptor = api.get(
        f"/v1/cotizaciones/{decision['cotizacionId']}/aceptacion", headers=SUSCRIPTOR
    )
    assert lectura_suscriptor.status_code == 200


def test_errores_de_negocio(api: TestClient, sinteticos: Any, dobles: Any) -> None:
    aprobada = _decidir(api, sinteticos, "S01")
    rechazada = _decidir(api, sinteticos, "S02")

    no_aprobada = _aceptar(api, rechazada)
    assert (no_aprobada.status_code, no_aprobada.json()["codigo"]) == (409, "DECISION_NO_APROBADA")

    texto = _aceptar(api, aprobada, versionDeclaraciones="2026.09.1")
    assert (texto.status_code, texto.json()["codigo"]) == (422, "TEXTO_NO_VIGENTE")

    incompletas = _aceptar(
        api, aprobada, declaraciones={**TODAS, "leidoCondiciones": False, "informacionVeraz": False}
    )
    assert (incompletas.status_code, incompletas.json()["codigo"]) == (
        422,
        "DECLARACIONES_INCOMPLETAS",
    )
    assert incompletas.json()["detalles"] == {"campos": ["leidoCondiciones", "informacionVeraz"]}

    otra_decision = _aceptar(api, {**aprobada, "id": str(uuid4())})
    assert (otra_decision.status_code, otra_decision.json()["codigo"]) == (
        404,
        "DECISION_NO_ENCONTRADA",
    )

    clave = str(uuid4())
    primera = _aceptar(api, aprobada, clave)
    conflicto = _aceptar(api, aprobada, clave, declaraciones={**TODAS, "avisosElectronicos": True})
    assert (conflicto.status_code, conflicto.json()["codigo"]) == (422, "IDEMPOTENCIA_CONFLICTO")

    ya = _aceptar(api, aprobada)
    assert (ya.status_code, ya.json()["codigo"]) == (409, "OFERTA_YA_ACEPTADA")
    assert ya.json()["detalles"] == {"aceptacionId": primera.json()["id"]}


@pytest.mark.parametrize("despues", [timedelta(0), timedelta(days=8)], ids=str)
def test_oferta_vencida(api: TestClient, sinteticos: Any, dobles: Any, despues: timedelta) -> None:
    decision = _decidir(api, sinteticos, "S01")
    dobles.reloj.instante = dobles.decisiones.decisiones[0].insumos.vigente_hasta + despues
    vencida = _aceptar(api, decision)
    assert vencida.status_code == 409
    assert (vencida.json()["codigo"], vencida.json()["detalles"]) == (
        "OFERTA_VENCIDA",
        {"accion": "NUEVA_COTIZACION"},
    )
    assert dobles.aceptaciones.aceptaciones == []


def test_aceptacion_no_encontrada(api: TestClient) -> None:
    respuesta = api.get(f"/v1/cotizaciones/{uuid4()}/aceptacion", headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "ACEPTACION_NO_ENCONTRADA")


def test_actores_y_validacion(api: TestClient, sinteticos: Any) -> None:
    decision = _decidir(api, sinteticos, "S01")
    suscriptor = _aceptar(api, decision, actor=SUSCRIPTOR)
    assert (suscriptor.status_code, suscriptor.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")
    sin_actor = api.post(
        f"/v1/cotizaciones/{decision['cotizacionId']}/aceptacion",
        json={},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert (sin_actor.status_code, sin_actor.json()["codigo"]) == (401, "ACTOR_REQUERIDO")
    sin_clave = api.post(
        f"/v1/cotizaciones/{decision['cotizacionId']}/aceptacion",
        json={"decisionId": decision["id"], "versionDeclaraciones": "x", "declaraciones": TODAS},
        headers=ASESOR,
    )
    assert (sin_clave.status_code, sin_clave.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    sin_declaraciones = _aceptar(api, decision, declaraciones={"leidoCondiciones": True})
    assert sin_declaraciones.status_code == 422


def test_sin_puertos_responde_503() -> None:
    with TestClient(crear_app(Settings())) as cliente:
        respuesta = cliente.get(f"/v1/cotizaciones/{uuid4()}/aceptacion", headers=ASESOR)
        assert (respuesta.status_code, respuesta.json()["codigo"]) == (
            503,
            "DEPENDENCIA_NO_DISPONIBLE",
        )
        registro = cliente.post(
            f"/v1/cotizaciones/{uuid4()}/aceptacion",
            json={
                "decisionId": str(uuid4()),
                "versionDeclaraciones": "2026.10.1",
                "declaraciones": TODAS,
            },
            headers={**ASESOR, "Idempotency-Key": str(uuid4())},
        )
        assert registro.status_code == 503


def test_un_error_desconocido_no_se_traduce() -> None:
    with pytest.raises(RuntimeError):
        error_aceptacion(RuntimeError("x"))
