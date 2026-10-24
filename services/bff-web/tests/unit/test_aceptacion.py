"""UT-W15 (bff-web): rutas de aceptación del portal (HU-W15) con polizas simulado.

Solo `asesores` acepta y consulta (los demás grupos, 403 sin llamar a polizas); actor, rol,
clave y correlación hacia polizas; errores B2 del contrato que pasan tal cual y falla cerrada
(503 SERVICIO_NO_DISPONIBLE) para cualquier otra respuesta.
"""

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import aceptaciones as ruta
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.aceptacion import Aceptaciones
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
CLAVE = "00000000-0000-4000-c000-000000000015"
COTIZACION_ID = "00000000-0000-4000-a000-000000000001"
DECISION_ID = "00000000-0000-4000-e000-000000000001"
RUTA = f"/v1/cotizaciones/{COTIZACION_ID}/aceptacion"
DECLARACIONES = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": False,
    "avisosElectronicos": True,
}
SOLICITUD = {
    "decisionId": DECISION_ID,
    "versionDeclaraciones": "2026.10.1",
    "declaraciones": DECLARACIONES,
}
ACEPTACION: dict[str, Any] = {
    "id": "00000000-0000-4000-f000-000000000001",
    "cotizacionId": COTIZACION_ID,
    "ofertaId": None,
    "decisionId": DECISION_ID,
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "huellaCalculo": "sha256:calculo",
    "versionDeclaraciones": "2026.10.1",
    "declaraciones": DECLARACIONES,
    "canal": "PORTAL_ASESOR",
    "actorId": "sub-asesor",
    "aceptadaEn": "2026-10-24T15:00:00Z",
    "huellaEvidencia": "sha256:" + "a" * 64,
    "recaudoHabilitado": True,
    "siguientePaso": "FIRMA",
}


def _usuario(grupos: str, sub: str = "sub-asesor") -> dict[str, str]:
    return {
        "X-Authenticated-Groups": grupos,
        "X-Authenticated-Issuer": BACKOFFICE,
        "X-Authenticated-Sub": sub,
        "X-Correlation-Id": "corr-bff-w15",
    }


ASESOR = _usuario("[asesores]")
SUSCRIPTOR = _usuario("[suscriptores]", "sub-suscriptora")
OPERACION = _usuario("[operacion]")


class Simulado:
    def __init__(self) -> None:
        self.pedidos: list[httpx.Request] = []
        self.responder: Callable[[httpx.Request], httpx.Response] = self._nominal

    @staticmethod
    def _nominal(pedido: httpx.Request) -> httpx.Response:
        return httpx.Response(201 if pedido.method == "POST" else 200, json=ACEPTACION)

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        return self.responder(pedido)


@pytest.fixture
def polizas(monkeypatch: pytest.MonkeyPatch) -> Iterator[Simulado]:
    simulado = Simulado()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cliente = PolizasCliente("http://polizas:8080", 1500, httpx.MockTransport(simulado))
    app.dependency_overrides[ruta.aceptaciones] = lambda: Aceptaciones(cliente)
    yield simulado
    app.dependency_overrides.clear()


def _aceptar(encabezados: dict[str, str], cuerpo: Any = None) -> httpx.Response:
    return TestClient(app).post(
        RUTA, json=cuerpo or SOLICITUD, headers={**encabezados, "Idempotency-Key": CLAVE}
    )


def test_el_asesor_registra_la_aceptacion(polizas: Simulado) -> None:
    respuesta = _aceptar(ASESOR)
    assert (respuesta.status_code, respuesta.json()) == (201, ACEPTACION)
    [enviado] = polizas.pedidos
    assert (enviado.method, enviado.url.path) == ("POST", RUTA)
    assert (enviado.headers["X-Actor-Id"], enviado.headers["X-Actor-Rol"]) == (
        "sub-asesor",
        "ASESOR",
    )
    assert enviado.headers["Idempotency-Key"] == CLAVE
    assert enviado.headers["X-Correlation-Id"] == "corr-bff-w15"
    assert json.loads(enviado.content) == SOLICITUD


def test_la_repeticion_responde_200(polizas: Simulado) -> None:
    polizas.responder = lambda _: httpx.Response(200, json=ACEPTACION)
    assert _aceptar(ASESOR).status_code == 200


def test_consultar(polizas: Simulado) -> None:
    respuesta = TestClient(app).get(RUTA, headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()) == (200, ACEPTACION)
    [enviado] = polizas.pedidos
    assert (enviado.method, enviado.url.path, enviado.headers["X-Actor-Rol"]) == (
        "GET",
        RUTA,
        "ASESOR",
    )


@pytest.mark.parametrize(
    ("encabezados", "estado", "codigo"),
    [
        (SUSCRIPTOR, 403, "ROL_NO_AUTORIZADO"),
        (OPERACION, 403, "ROL_NO_AUTORIZADO"),
        ({**ASESOR, "X-Authenticated-Issuer": "https://otro"}, 403, "ROL_NO_AUTORIZADO"),
        ({k: v for k, v in ASESOR.items() if k != "X-Authenticated-Sub"}, 401, "ACTOR_REQUERIDO"),
    ],
)
def test_solo_asesores(
    polizas: Simulado, encabezados: dict[str, str], estado: int, codigo: str
) -> None:
    for respuesta in (_aceptar(encabezados), TestClient(app).get(RUTA, headers=encabezados)):
        assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
    assert polizas.pedidos == []


def test_validacion_antes_de_llamar(polizas: Simulado) -> None:
    sin_clave = TestClient(app).post(RUTA, json=SOLICITUD, headers=ASESOR)
    assert (sin_clave.status_code, sin_clave.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    incompleta = _aceptar(ASESOR, {"decisionId": DECISION_ID})
    assert incompleta.status_code == 422
    assert polizas.pedidos == []


@pytest.mark.parametrize(
    ("estado", "codigo", "detalles"),
    [
        (409, "OFERTA_VENCIDA", {"accion": "NUEVA_COTIZACION"}),
        (409, "DECISION_NO_APROBADA", {}),
        (409, "OFERTA_YA_ACEPTADA", {"aceptacionId": ACEPTACION["id"]}),
        (422, "DECLARACIONES_INCOMPLETAS", {"campos": ["leidoCondiciones"]}),
        (422, "TEXTO_NO_VIGENTE", {}),
        (422, "IDEMPOTENCIA_CONFLICTO", {}),
        (404, "DECISION_NO_ENCONTRADA", {}),
    ],
)
def test_errores_del_contrato_pasan(
    polizas: Simulado, estado: int, codigo: str, detalles: dict[str, Any]
) -> None:
    polizas.responder = lambda _: httpx.Response(
        estado, json={"codigo": codigo, "mensaje": "m", "detalles": detalles}
    )
    respuesta = _aceptar(ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
    assert respuesta.json()["detalles"] == detalles
    assert respuesta.json()["correlationId"] == "corr-bff-w15"


def test_aceptacion_no_encontrada_pasa(polizas: Simulado) -> None:
    polizas.responder = lambda _: httpx.Response(
        404, json={"codigo": "ACEPTACION_NO_ENCONTRADA", "mensaje": "m", "detalles": {}}
    )
    respuesta = TestClient(app).get(RUTA, headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "ACEPTACION_NO_ENCONTRADA")


def _caido(_: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("polizas caído")


@pytest.mark.parametrize(
    "responder",
    [
        lambda _: httpx.Response(
            503, json={"codigo": "DEPENDENCIA_NO_DISPONIBLE", "mensaje": "m", "detalles": {}}
        ),
        lambda _: httpx.Response(500, json={"codigo": "ERROR_INTERNO"}),
        lambda _: httpx.Response(409, json={"codigo": "OTRO_CODIGO"}),
        lambda _: httpx.Response(502, text="<html>"),
        lambda _: httpx.Response(201, json={**ACEPTACION, "huellaEvidencia": "md5:x"}),
        lambda _: httpx.Response(201, json={"id": "x"}),
        _caido,
    ],
    ids=[
        "503-dependencia",
        "500",
        "codigo-desconocido",
        "no-json",
        "fuera-de-dto",
        "vacio",
        "caido",
    ],
)
def test_falla_cerrada(polizas: Simulado, responder: Callable[..., httpx.Response]) -> None:
    polizas.responder = responder
    for respuesta in (_aceptar(ASESOR), TestClient(app).get(RUTA, headers=ASESOR)):
        assert (respuesta.status_code, respuesta.json()["codigo"]) == (
            503,
            "SERVICIO_NO_DISPONIBLE",
        )


def test_el_servicio_por_omision_usa_la_configuracion() -> None:
    ruta.aceptaciones.cache_clear()
    servicio = ruta.aceptaciones()
    assert servicio.polizas.url == settings.polizas_url.rstrip("/")
    assert servicio.polizas.timeout == settings.polizas_timeout_ms / 1000
    ruta.aceptaciones.cache_clear()
