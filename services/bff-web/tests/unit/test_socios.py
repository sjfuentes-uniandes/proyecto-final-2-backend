import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import socios as ruta
from bff_web.adapters.outbound.api_socios import ApiSocios
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient
from solventa_common.logging import FormateadorJson

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
CLIENTES = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_cli"
ADMIN_SOCIOS = {"X-Authenticated-Groups": "[administradores-socios]", "X-Authenticated-Issuer": BACKOFFICE,
                "X-Authenticated-Sub": "sub-paula"}
SOCIO = {"id": "SOC-000001", "nombre": "Fintech Caribe S.A.S.", "canal": "plataforma_digital", "plan": "basico",
         "permisos": ["cotizar"], "estado": "activo", "clientId": "abc123", "creado": "2026-10-12T10:00:00Z",
         "desactivado": None}
CREADO = SOCIO | {"clientSecret": "secreto-unico-xyz", "apiKey": "api-key-unica-xyz"}
ALTA = {"nombre": "Fintech Caribe S.A.S.", "canal": "plataforma_digital", "plan": "basico", "permisos": ["cotizar"]}


class ApiSociosFalsa:
    def __init__(self) -> None:
        self.solicitudes: list[httpx.Request] = []
        self.status: int | None = None
        self.caido = False

    def __call__(self, solicitud: httpx.Request) -> httpx.Response:
        self.solicitudes.append(solicitud)
        if self.caido:
            raise httpx.ConnectError("sin conexión", request=solicitud)
        if self.status:
            return httpx.Response(self.status, json={"codigo": "ERROR_INTERNO", "mensaje": "x", "detalles": {}})
        ruta_admin = solicitud.url.path
        if solicitud.method == "POST" and ruta_admin == "/admin/socios":
            return httpx.Response(201, json=CREADO)
        if ruta_admin == "/admin/socios":
            return httpx.Response(200, json=[SOCIO])
        if ruta_admin.endswith("/desactivar"):
            return httpx.Response(200, json=SOCIO | {"estado": "desactivado", "desactivado": "2026-10-12T11:00:00Z"})
        if ruta_admin == "/admin/socios/SOC-000001":
            return httpx.Response(200, json=SOCIO)
        return httpx.Response(404, json={"codigo": "SOCIO_NO_ENCONTRADO", "mensaje": "El socio no existe",
                                         "correlationId": "c-1", "detalles": {}})


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[ApiSociosFalsa]:
    falsa = ApiSociosFalsa()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE, "clientes": CLIENTES})
    app.dependency_overrides[ruta.api_socios] = lambda: ApiSocios("http://api-socios:8080", httpx.MockTransport(falsa))
    yield falsa
    app.dependency_overrides.clear()


cliente = TestClient(app)


@pytest.mark.parametrize("encabezados", [
    {},
    {"X-Authenticated-Groups": "[operacion, administradores]", "X-Authenticated-Issuer": BACKOFFICE},
    {"X-Authenticated-Groups": "[administradores-socios]", "X-Authenticated-Issuer": CLIENTES},
])
def test_solo_administradores_de_socios_del_backoffice(api: ApiSociosFalsa, encabezados: dict[str, str]) -> None:
    for metodo, url in (("GET", "/socios"), ("POST", "/socios"), ("GET", "/socios/SOC-000001"),
                        ("POST", "/socios/SOC-000001/desactivar")):
        respuesta = cliente.request(metodo, url, json=ALTA, headers=encabezados)
        assert respuesta.status_code == 403 and respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"
    assert not api.solicitudes


def test_alta_reenvia_y_devuelve_credenciales_sin_cache(api: ApiSociosFalsa) -> None:
    respuesta = cliente.post("/socios", json=ALTA, headers=ADMIN_SOCIOS)
    assert respuesta.status_code == 201
    assert respuesta.headers["cache-control"] == "no-store"
    assert respuesta.json()["clientSecret"] == "secreto-unico-xyz"
    (enviada,) = api.solicitudes
    assert (enviada.method, str(enviada.url)) == ("POST", "http://api-socios:8080/admin/socios")
    assert json.loads(enviada.content) == ALTA
    assert enviada.headers["X-Authenticated-Sub"] == "sub-paula"
    assert "x-partner-client-id" not in enviada.headers


def test_listado_y_detalle_no_incluyen_secretos(api: ApiSociosFalsa) -> None:
    listado = cliente.get("/socios", headers=ADMIN_SOCIOS)
    detalle = cliente.get("/socios/SOC-000001", headers=ADMIN_SOCIOS)
    assert listado.json()[0]["clientId"] == "abc123"
    for respuesta in (listado, detalle):
        assert respuesta.status_code == 200
        assert "clientSecret" not in respuesta.text and "apiKey" not in respuesta.text


def test_desactivar(api: ApiSociosFalsa) -> None:
    respuesta = cliente.post("/socios/SOC-000001/desactivar", headers=ADMIN_SOCIOS)
    assert respuesta.status_code == 200 and respuesta.json()["estado"] == "desactivado"
    assert api.solicitudes[0].url.path == "/admin/socios/SOC-000001/desactivar"


@pytest.mark.parametrize("cuerpo", [ALTA | {"permisos": []}, ALTA | {"plan": "oro"}, ALTA | {"apiKey": "x"}])
def test_alta_invalida_no_llega_a_api_socios(api: ApiSociosFalsa, cuerpo: dict[str, Any]) -> None:
    respuesta = cliente.post("/socios", json=cuerpo, headers=ADMIN_SOCIOS)
    assert respuesta.status_code == 422 and respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert respuesta.json()["detalles"]["campos"]
    assert not api.solicitudes


def test_errores_de_api_socios_en_formato_estable(api: ApiSociosFalsa) -> None:
    no_existe = cliente.get("/socios/SOC-000009", headers=ADMIN_SOCIOS)
    assert no_existe.status_code == 404
    assert set(no_existe.json()) == {"codigo", "mensaje", "correlationId", "detalles"}
    assert no_existe.json()["codigo"] == "SOCIO_NO_ENCONTRADO"
    assert cliente.get("/socios/otro", headers=ADMIN_SOCIOS).json()["codigo"] == "SOLICITUD_INVALIDA"
    api.status = 500
    caida = cliente.get("/socios", headers=ADMIN_SOCIOS)
    assert caida.status_code == 503 and caida.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    api.status, api.caido = None, True
    assert cliente.post("/socios", json=ALTA, headers=ADMIN_SOCIOS).json()["codigo"] == "SERVICIO_NO_DISPONIBLE"


def test_las_rutas_de_hu_w27_conservan_su_formato() -> None:
    respuesta = cliente.get("/backoffice/usuarios", headers={"X-Authenticated-Groups": "[operacion]"})
    assert respuesta.status_code == 403 and "detail" in respuesta.json()


def test_secreto_ausente_de_los_logs_del_bff(api: ApiSociosFalsa) -> None:
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level
    salida = io.StringIO()
    handler = logging.StreamHandler(salida)
    handler.setFormatter(FormateadorJson("bff-web", "pruebas"))
    raiz.handlers, raiz.level = [handler], logging.DEBUG
    try:
        assert cliente.post("/socios", json=ALTA, headers=ADMIN_SOCIOS).status_code == 201
    finally:
        raiz.handlers, raiz.level = anteriores, nivel
    logs = salida.getvalue()
    assert "POST /socios" in logs
    assert "secreto-unico-xyz" not in logs and "api-key-unica-xyz" not in logs
