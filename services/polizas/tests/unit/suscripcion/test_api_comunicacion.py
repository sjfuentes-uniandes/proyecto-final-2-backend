"""API /v1/suscripciones con `comunicacion` (HU-W14, W14-B02) sobre los dobles de los puertos:
criterios técnicos 1 a 6 de §2.2 por HTTP, en el POST y en el GET."""

import json
import re
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from polizas.domain.suscripcion.errores import DependenciaNoDisponible

BASE = "/v1/suscripciones"
CODIGO_REGLA = re.compile(r"SUS-\d{3}")
ALTERNATIVAS = [
    {"productoId": "vida-senior", "descriptor": {"codigo": "EDAD_MAXIMA", "valor": "80"}},
    {"productoId": "proteccion-pagos", "descriptor": {"codigo": "SIN_LIMITE_EDAD", "valor": None}},
    {"productoId": "auxilio-funerario", "descriptor": {"codigo": "EDAD_MAXIMA", "valor": "85"}},
]


def _cabeceras(rol: str = "ASESOR") -> dict[str, str]:
    return {"X-Actor-Id": f"sub-{rol.lower()}", "X-Actor-Rol": rol}


def _solicitar(api: TestClient, sinteticos: Any, caso_id: str) -> Any:
    return api.post(
        BASE,
        json={"cotizacionId": sinteticos.cotizacion_id(caso_id)},
        headers={**_cabeceras(), "Idempotency-Key": str(uuid4())},
    )


def _leer(api: TestClient, decision_id: str, rol: str = "ASESOR") -> Any:
    return api.get(f"{BASE}/{decision_id}", headers=_cabeceras(rol))


def test_aprobada_vigente_en_el_post_y_en_el_get(api: TestClient, sinteticos: Any) -> None:
    """Criterio técnico 1 (AC1)."""
    creada = _solicitar(api, sinteticos, "S01").json()
    esperada = {
        "condicion": "APROBADA_SIN_AJUSTES",
        "admiteContratacion": True,
        "siguientePaso": "ACEPTAR_OFERTA",
        "vigenteHasta": creada["condiciones"]["vigenteHasta"],
        "motivoCliente": None,
        "alternativas": [],
        "plazoRespuestaHoras": None,
    }
    assert creada["comunicacion"] == esperada
    assert _leer(api, creada["id"]).json()["comunicacion"] == esperada


@pytest.mark.parametrize(
    ("caso_id", "motivo"),
    [("S02", "EDAD_FUERA_DE_RANGO"), ("S03", "PLAZO_FUERA_DE_RANGO"), ("S04", "NO_ELEGIBLE")],
)
def test_rechazada_con_motivo_y_alternativas(
    api: TestClient, sinteticos: Any, caso_id: str, motivo: str
) -> None:
    """Criterio técnico 2 (AC2): sin contratación, con la explicación permitida."""
    comunicacion = _solicitar(api, sinteticos, caso_id).json()["comunicacion"]
    assert comunicacion == {
        "condicion": "NO_ASEGURABLE",
        "admiteContratacion": False,
        "siguientePaso": "COTIZAR_ALTERNATIVA",
        "vigenteHasta": None,
        "motivoCliente": motivo,
        "alternativas": ALTERNATIVAS,
        "plazoRespuestaHoras": None,
    }


def test_rechazo_por_regla_reservada_sin_rastro_del_valor_ni_del_codigo(
    api: TestClient, sinteticos: Any
) -> None:
    """Criterio técnico 2 (SUS-015): NO_ELEGIBLE, ni el valor ni el código en la comunicación,
    para el asesor y para el suscriptor."""
    creada = _solicitar(api, sinteticos, "S04").json()
    for rol in ("ASESOR", "SUSCRIPTOR"):
        texto = json.dumps(_leer(api, creada["id"], rol).json()["comunicacion"])
        assert not CODIGO_REGLA.search(texto)
        assert "coincidencias" not in texto.lower()
        assert "NO_ELEGIBLE" in texto


@pytest.mark.parametrize(
    ("caso_id", "motivo"),
    [
        ("S05", "SUMA_REQUIERE_REVISION"),
        ("S06", "SUMA_REQUIERE_REVISION"),
        ("S07", "PERFIL_REQUIERE_REVISION"),
        ("S08", "PERFIL_REQUIERE_REVISION"),
    ],
)
def test_revision_con_siguiente_paso_sin_prometer_cobertura(
    api: TestClient, sinteticos: Any, caso_id: str, motivo: str
) -> None:
    """Criterio técnico 3 (AC3): plazo de 8 h, sin vigencia, alternativas ni contratación."""
    comunicacion = _solicitar(api, sinteticos, caso_id).json()["comunicacion"]
    assert comunicacion == {
        "condicion": "EN_REVISION",
        "admiteContratacion": False,
        "siguientePaso": "ESPERAR_REVISION",
        "vigenteHasta": None,
        "motivoCliente": motivo,
        "alternativas": [],
        "plazoRespuestaHoras": 8,
    }


def test_aprobada_vencida_al_leer(api: TestClient, sinteticos: Any, dobles: Any) -> None:
    """Criterio técnico 4: la decisión no cambia, su comunicación sí."""
    creada = _solicitar(api, sinteticos, "S01").json()
    dobles.reloj.instante = sinteticos.insumos("S01").vigente_hasta + timedelta(seconds=1)
    leida = _leer(api, creada["id"]).json()
    assert leida["resultado"] == "APROBADA"
    assert (
        leida["comunicacion"]["admiteContratacion"],
        leida["comunicacion"]["siguientePaso"],
        leida["comunicacion"]["vigenteHasta"],
    ) == (False, "COTIZAR_NUEVAMENTE", creada["condiciones"]["vigenteHasta"])
    sin_comunicacion = {k: v for k, v in leida.items() if k != "comunicacion"}
    assert sin_comunicacion == {k: v for k, v in creada.items() if k != "comunicacion"}


def test_la_repeticion_idempotente_tambien_comunica(api: TestClient, sinteticos: Any) -> None:
    clave = str(uuid4())
    cuerpo = {"cotizacionId": sinteticos.cotizacion_id("S02")}
    cabeceras = {**_cabeceras(), "Idempotency-Key": clave}
    primera = api.post(BASE, json=cuerpo, headers=cabeceras)
    segunda = api.post(BASE, json=cuerpo, headers=cabeceras)
    assert (primera.status_code, segunda.status_code) == (201, 200)
    assert segunda.json()["comunicacion"] == primera.json()["comunicacion"]


def test_catalogo_caido_al_leer_devuelve_la_decision_sin_alternativas(
    api: TestClient, sinteticos: Any, dobles: Any
) -> None:
    """Falla abierta solo para las alternativas: 200 con la decisión y `alternativas = []`."""
    creada = _solicitar(api, sinteticos, "S02").json()
    dobles.reglas.falla = DependenciaNoDisponible("catalogo")
    respuesta = _leer(api, creada["id"])
    assert respuesta.status_code == 200
    comunicacion = respuesta.json()["comunicacion"]
    assert (comunicacion["motivoCliente"], comunicacion["alternativas"]) == (
        "EDAD_FUERA_DE_RANGO",
        [],
    )


def test_decision_inexistente_sigue_en_404(api: TestClient) -> None:
    """Criterio técnico 6."""
    respuesta = _leer(api, str(uuid4()))
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "DECISION_NO_ENCONTRADA")
