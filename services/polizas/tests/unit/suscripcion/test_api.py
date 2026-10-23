"""API /v1/suscripciones (HU-W13) con los dobles de los puertos: rutas, actores y roles, DS2-07,
códigos de error estables (SEC-W13-01) e idempotencia por HTTP."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from polizas.config import Settings
from polizas.domain.suscripcion.errores import DependenciaNoDisponible
from polizas.main import crear_app

BASE = "/v1/suscripciones"


def _cabeceras(rol: str = "ASESOR", clave: str | None = None) -> dict[str, str]:
    cabeceras = {"X-Actor-Id": f"sub-{rol.lower()}", "X-Actor-Rol": rol}
    if clave is not None:
        cabeceras["Idempotency-Key"] = clave
    return cabeceras


def _solicitar(api: TestClient, sinteticos: Any, id_caso: str, clave: str | None = None) -> Any:
    return api.post(
        BASE,
        json={"cotizacionId": sinteticos.cotizacion_id(id_caso)},
        headers=_cabeceras(clave=clave or str(uuid4())),
    )


def test_aprobada_201_con_seis_reglas_y_la_version(api: TestClient, sinteticos: Any) -> None:
    """Criterio técnico 1 (AC1)."""
    respuesta = _solicitar(api, sinteticos, "S01")
    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["resultado"] == "APROBADA"
    assert [r["resultado"] for r in cuerpo["reglasEvaluadas"]] == ["CUMPLE"] * 6
    assert (cuerpo["versionReglas"], cuerpo["hashReglas"]) == (
        "2026.10.1",
        sinteticos.reglas().hash,
    )
    assert cuerpo["codigo"] == "DEC-2026-000001"
    assert cuerpo["tipo"] == "AUTOMATICA" and cuerpo["admiteExcepcion"] is False
    assert cuerpo["condiciones"]["sumaAsegurada"] == "180000000"
    assert cuerpo["condiciones"]["vigenteHasta"] == "2026-10-26T14:00:00Z"
    assert cuerpo["contexto"] == {
        "nivelRiesgo": "BAJO",
        "confianza": "0.92",
        "fuentesConsultadas": 4,
        "fuentesDeclaradas": 4,
    }
    assert "edad" not in cuerpo["condiciones"]
    assert respuesta.headers["X-Correlation-Id"]


def test_rechazada_y_revision(api: TestClient, sinteticos: Any) -> None:
    """Criterios técnicos 2 y 3 (AC2, AC3)."""
    edad = _solicitar(api, sinteticos, "S02").json()
    assert (edad["resultado"], edad["reglaDeterminante"], edad["motivos"]) == (
        "RECHAZADA",
        "SUS-001",
        ["EDAD_FUERA_DE_RANGO"],
    )
    plazo = _solicitar(api, sinteticos, "S03").json()
    assert (plazo["resultado"], plazo["reglaDeterminante"]) == ("RECHAZADA", "SUS-019")
    revision = _solicitar(api, sinteticos, "S05").json()
    assert revision["resultado"] == "REVISION_REQUERIDA"
    no_cumplen = {r["codigo"] for r in revision["reglasEvaluadas"] if r["resultado"] == "NO_CUMPLE"}
    assert no_cumplen == {"SUS-004", "SUS-011"}
    sin_perfil = _solicitar(api, sinteticos, "S08").json()
    assert sin_perfil["resultado"] == "REVISION_REQUERIDA"
    assert sin_perfil["contexto"] == {
        "nivelRiesgo": None,
        "confianza": None,
        "fuentesConsultadas": None,
        "fuentesDeclaradas": None,
    }


def test_la_regla_reservada_no_muestra_su_valor_al_asesor(api: TestClient, sinteticos: Any) -> None:
    """DS2-07: el asesor no ve el valor de SUS-015; el suscriptor sí."""
    asesor = _solicitar(api, sinteticos, "S04").json()
    [sus015] = [r for r in asesor["reglasEvaluadas"] if r["codigo"] == "SUS-015"]
    assert (sus015["valorCaso"], sus015["reservada"], sus015["resultado"]) == (
        None,
        True,
        "NO_CUMPLE",
    )
    assert asesor["motivos"] == ["NO_ELEGIBLE"]
    suscriptor = api.get(f"{BASE}/{asesor['id']}", headers=_cabeceras("SUSCRIPTOR")).json()
    [visible] = [r for r in suscriptor["reglasEvaluadas"] if r["codigo"] == "SUS-015"]
    assert (visible["valorCaso"], visible["reservada"]) == ("1", False)
    otras = [r for r in suscriptor["reglasEvaluadas"] if r["codigo"] != "SUS-015"]
    assert all(r["reservada"] is False for r in otras)


def test_idempotencia_por_http(api: TestClient, sinteticos: Any) -> None:
    """Criterio técnico 4: misma clave → 200 y el mismo cuerpo; otra clave → 200 y la misma
    decisión; misma clave con otra cotización → 422 IDEMPOTENCIA_CONFLICTO."""
    clave = str(uuid4())
    primera = _solicitar(api, sinteticos, "S01", clave)
    repetida = _solicitar(api, sinteticos, "S01", clave)
    otra_clave = _solicitar(api, sinteticos, "S01")
    assert (primera.status_code, repetida.status_code, otra_clave.status_code) == (201, 200, 200)
    assert repetida.json() == primera.json() == otra_clave.json()
    conflicto = _solicitar(api, sinteticos, "S02", clave)
    assert (conflicto.status_code, conflicto.json()["codigo"]) == (422, "IDEMPOTENCIA_CONFLICTO")


@pytest.mark.parametrize(
    ("sin_decision", "estado", "codigo"),
    [
        ("S09", 409, "OFERTA_VENCIDA"),
        ("S10", 403, "IDENTIDAD_NO_VERIFICADA"),
        ("S11", 409, "IDENTIDAD_PENDIENTE"),
    ],
)
def test_precondiciones(
    api: TestClient, sinteticos: Any, dobles: Any, sin_decision: str, estado: int, codigo: str
) -> None:
    """Criterio técnico 5: sin decisión."""
    respuesta = _solicitar(api, sinteticos, sin_decision)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
    if codigo == "OFERTA_VENCIDA":
        assert respuesta.json()["detalles"] == {"accion": "NUEVA_COTIZACION"}
    assert dobles.decisiones.decisiones == []


def test_cotizacion_inexistente_y_sin_reglas(api: TestClient, dobles: Any) -> None:
    inexistente = api.post(
        BASE, json={"cotizacionId": str(uuid4())}, headers=_cabeceras(clave=str(uuid4()))
    )
    assert (inexistente.status_code, inexistente.json()["codigo"]) == (
        404,
        "COTIZACION_NO_ENCONTRADA",
    )
    dobles.reglas.vigente = None
    otra = api.post(
        BASE,
        json={"cotizacionId": "00000000-0000-4000-a000-000000000001"},
        headers=_cabeceras(clave=str(uuid4())),
    )
    assert (otra.status_code, otra.json()["codigo"]) == (422, "REGLAS_NO_VIGENTES")


def test_dependencia_caida_503_sin_detalles_internos(
    api: TestClient, sinteticos: Any, dobles: Any
) -> None:
    dobles.reglas.falla = DependenciaNoDisponible("catalogo")
    respuesta = _solicitar(api, sinteticos, "S01")
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {"dependencia": "catalogo"}
    assert dobles.decisiones.decisiones == []


@pytest.mark.parametrize(
    ("cabeceras", "estado", "codigo"),
    [
        ({}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": "x"}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": "x", "X-Actor-Rol": "SOCIO"}, 403, "ROL_NO_AUTORIZADO"),
        ({"X-Actor-Id": "x", "X-Actor-Rol": "SUSCRIPTOR"}, 403, "ROL_NO_AUTORIZADO"),
    ],
)
def test_actor_y_rol_para_solicitar(
    api: TestClient, sinteticos: Any, cabeceras: dict[str, str], estado: int, codigo: str
) -> None:
    respuesta = api.post(
        BASE,
        json={"cotizacionId": sinteticos.cotizacion_id("S01")},
        headers={**cabeceras, "Idempotency-Key": str(uuid4())},
    )
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)


def test_solicitud_invalida(api: TestClient, sinteticos: Any) -> None:
    sin_clave = api.post(
        BASE, json={"cotizacionId": sinteticos.cotizacion_id("S01")}, headers=_cabeceras()
    )
    assert (sin_clave.status_code, sin_clave.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert "Idempotency-Key" in sin_clave.json()["detalles"]["campos"]
    mala = api.post(BASE, json={"cotizacionId": "x"}, headers=_cabeceras(clave=str(uuid4())))
    assert mala.json()["detalles"]["campos"] == ["cotizacionId"]


def test_consultas_por_id_y_por_cotizacion(api: TestClient, sinteticos: Any) -> None:
    decision = _solicitar(api, sinteticos, "S01").json()
    por_id = api.get(f"{BASE}/{decision['id']}", headers=_cabeceras())
    assert por_id.json() == decision
    lista = api.get(BASE, params={"cotizacionId": decision["cotizacionId"]}, headers=_cabeceras())
    assert [i["id"] for i in lista.json()["items"]] == [decision["id"]]
    vacia = api.get(BASE, params={"cotizacionId": str(uuid4())}, headers=_cabeceras("SUSCRIPTOR"))
    assert vacia.json() == {"items": []}
    no_existe = api.get(f"{BASE}/{uuid4()}", headers=_cabeceras())
    assert (no_existe.status_code, no_existe.json()["codigo"]) == (404, "DECISION_NO_ENCONTRADA")


def test_bandeja_y_resumen_solo_para_suscriptores(
    api: TestClient, sinteticos: Any, dobles: Any
) -> None:
    dobles.reloj.instante = sinteticos.ahora - timedelta(hours=9)
    _solicitar(api, sinteticos, "S05")
    dobles.reloj.instante = sinteticos.ahora
    _solicitar(api, sinteticos, "S06")
    _solicitar(api, sinteticos, "S01")
    assert api.get(f"{BASE}/bandeja", headers=_cabeceras()).status_code == 403
    bandeja = api.get(f"{BASE}/bandeja", params={"limite": 1}, headers=_cabeceras("SUSCRIPTOR"))
    cuerpo = bandeja.json()
    assert (bandeja.status_code, cuerpo["total"], len(cuerpo["items"])) == (200, 2, 1)
    assert cuerpo["items"][0]["antiguedadMinutos"] == 540
    assert cuerpo["items"][0]["sumaAsegurada"] == "620000000"
    siguiente = api.get(
        f"{BASE}/bandeja",
        params={"cursor": cuerpo["siguienteCursor"]},
        headers=_cabeceras("SUSCRIPTOR"),
    ).json()
    assert [i["reglasNoCumplidas"] for i in siguiente["items"]] == [["SUS-004"]]
    invalido = api.get(f"{BASE}/bandeja", params={"cursor": "x"}, headers=_cabeceras("SUSCRIPTOR"))
    assert (invalido.status_code, invalido.json()["detalles"]) == (422, {"campos": ["cursor"]})
    filtrada = api.get(
        f"{BASE}/bandeja",
        params={"motivo": "PERFIL_REQUIERE_REVISION", "productoId": "vida-hipotecario"},
        headers=_cabeceras("SUSCRIPTOR"),
    ).json()
    assert filtrada["total"] == 1
    dia = {"desde": "2026-10-22T05:00:00Z", "hasta": "2026-10-23T05:00:00Z"}
    assert api.get(f"{BASE}/resumen", params=dia, headers=_cabeceras()).status_code == 403
    resumen = api.get(f"{BASE}/resumen", params=dia, headers=_cabeceras("SUSCRIPTOR")).json()
    assert (resumen["resueltasAutomaticamente"], resumen["enviadasARevision"]) == (1, 2)
    assert (resumen["fueraDelCompromiso"], resumen["compromisoRevisionHoras"]) == (1, 8)
    vacio = api.get(
        f"{BASE}/resumen",
        params={"desde": dia["hasta"], "hasta": dia["desde"]},
        headers=_cabeceras("SUSCRIPTOR"),
    )
    assert (vacio.status_code, vacio.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")


def test_reconstruccion(api: TestClient, sinteticos: Any, dobles: Any) -> None:
    decision = _solicitar(api, sinteticos, "S02").json()
    ruta = f"{BASE}/{decision['id']}/reconstruccion"
    respuesta = api.get(ruta, headers=_cabeceras("SUSCRIPTOR")).json()
    assert respuesta == {
        "coincide": True,
        "resultadoOriginal": "RECHAZADA",
        "resultadoReconstruido": "RECHAZADA",
        "versionReglas": "2026.10.1",
        "hashReglas": sinteticos.reglas().hash,
    }
    assert api.get(f"{BASE}/{uuid4()}/reconstruccion", headers=_cabeceras()).status_code == 404
    dobles.reglas.falla = DependenciaNoDisponible("catalogo")
    caida = api.get(ruta, headers=_cabeceras())
    assert (caida.status_code, caida.json()["codigo"]) == (503, "DEPENDENCIA_NO_DISPONIBLE")


def test_sin_puertos_responde_503(sinteticos: Any) -> None:
    """Sin base configurada no hay casos de uso: 503, nunca 500."""
    with TestClient(crear_app(Settings())) as api:
        respuesta = _solicitar(api, sinteticos, "S01")
        assert respuesta.status_code == 503
        assert api.get(f"{BASE}/{uuid4()}", headers=_cabeceras()).status_code == 503
        reconstruccion = api.get(f"{BASE}/{uuid4()}/reconstruccion", headers=_cabeceras())
        assert reconstruccion.status_code == 503
        assert api.get("/health").json() == {"status": "ok"}
