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
ADMIN_SOCIOS = {
    "X-Authenticated-Groups": "[administradores-socios]",
    "X-Authenticated-Issuer": BACKOFFICE,
    "X-Authenticated-Sub": "sub-paula",
}
SOCIO = {
    "id": "SOC-000001",
    "nombre": "Fintech Caribe S.A.S.",
    "canal": "plataforma_digital",
    "plan": "basico",
    "permisos": ["cotizar"],
    "estado": "activo",
    "clientId": "abc123",
    "creado": "2026-10-12T10:00:00Z",
    "desactivado": None,
}
CREADO = SOCIO | {"clientSecret": "secreto-unico-xyz", "apiKey": "api-key-unica-xyz"}
ALTA = {
    "nombre": "Fintech Caribe S.A.S.",
    "canal": "plataforma_digital",
    "plan": "basico",
    "permisos": ["cotizar"],
}
LIMITES = {"plan": "prueba", "limitePorSegundo": 1.0, "rafaga": 2, "cuota": 5, "periodo": "DAY"}
CONSUMO = {
    "socioId": "SOC-000001",
    "limites": LIMITES,
    "ventana": {"inicio": "2026-10-13", "fin": "2026-10-13", "reinicio": "2026-10-14T00:00:00Z"},
    "usado": 5,
    "restante": 0,
    "porcentaje": 100,
    "estado": "agotada",
    "limitadas": 3,
}


class ApiSociosFalsa:
    def __init__(self) -> None:
        self.solicitudes: list[httpx.Request] = []
        self.status: int | None = None
        self.error_negocio: tuple[int, dict[str, Any]] | None = None
        self.caido = False

    def __call__(self, solicitud: httpx.Request) -> httpx.Response:
        self.solicitudes.append(solicitud)
        if self.caido:
            raise httpx.ConnectError("sin conexión", request=solicitud)
        if self.status:
            return httpx.Response(
                self.status, json={"codigo": "ERROR_INTERNO", "mensaje": "x", "detalles": {}}
            )
        if self.error_negocio:
            return httpx.Response(self.error_negocio[0], json=self.error_negocio[1])
        ruta_admin = solicitud.url.path
        if ruta_admin == "/admin/socios/planes":
            return httpx.Response(200, json=[LIMITES | {"plan": "basico", "cuota": 1000}, LIMITES])
        if ruta_admin == "/admin/socios/SOC-000001/plan":
            return httpx.Response(200, json=SOCIO | json.loads(solicitud.content))
        if ruta_admin == "/admin/socios/SOC-000001/consumo":
            return httpx.Response(200, json=CONSUMO | {"apiKeyId": "key-que-no-debe-salir"})
        if solicitud.method == "POST" and ruta_admin == "/admin/socios":
            return httpx.Response(201, json=CREADO)
        if ruta_admin == "/admin/socios":
            return httpx.Response(200, json=[SOCIO])
        if ruta_admin.endswith("/desactivar"):
            return httpx.Response(
                200, json=SOCIO | {"estado": "desactivado", "desactivado": "2026-10-12T11:00:00Z"}
            )
        if ruta_admin == "/admin/socios/SOC-000001":
            return httpx.Response(200, json=SOCIO)
        return httpx.Response(
            404,
            json={
                "codigo": "SOCIO_NO_ENCONTRADO",
                "mensaje": "El socio no existe",
                "correlationId": "c-1",
                "detalles": {},
            },
        )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[ApiSociosFalsa]:
    falsa = ApiSociosFalsa()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE, "clientes": CLIENTES})
    app.dependency_overrides[ruta.api_socios] = lambda: ApiSocios(
        "http://api-socios:8080", httpx.MockTransport(falsa)
    )
    yield falsa
    app.dependency_overrides.clear()


cliente = TestClient(app)


@pytest.mark.parametrize(
    "encabezados",
    [
        {},
        {
            "X-Authenticated-Groups": "[operacion, administradores]",
            "X-Authenticated-Issuer": BACKOFFICE,
        },
        {"X-Authenticated-Groups": "[administradores-socios]", "X-Authenticated-Issuer": CLIENTES},
    ],
)
def test_solo_administradores_de_socios_del_backoffice(
    api: ApiSociosFalsa, encabezados: dict[str, str]
) -> None:
    for metodo, url in (
        ("GET", "/socios"),
        ("POST", "/socios"),
        ("GET", "/socios/SOC-000001"),
        ("POST", "/socios/SOC-000001/desactivar"),
        ("GET", "/socios/planes"),
        ("PUT", "/socios/SOC-000001/plan"),
        ("GET", "/socios/SOC-000001/consumo"),
    ):
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


@pytest.mark.parametrize(
    "cuerpo",
    [
        ALTA | {"permisos": []},
        ALTA | {"plan": "oro"},
        ALTA | {"apiKey": "x"},
        ALTA | {"nombre": " 5\r "},
    ],
)
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
    assert (
        cliente.get("/socios/otro", headers=ADMIN_SOCIOS).json()["codigo"] == "SOLICITUD_INVALIDA"
    )
    api.status = 500
    caida = cliente.get("/socios", headers=ADMIN_SOCIOS)
    assert caida.status_code == 503 and caida.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    api.status, api.caido = None, True
    assert (
        cliente.post("/socios", json=ALTA, headers=ADMIN_SOCIOS).json()["codigo"]
        == "SERVICIO_NO_DISPONIBLE"
    )


def test_las_rutas_de_hu_w27_conservan_su_formato() -> None:
    respuesta = cliente.get(
        "/backoffice/usuarios", headers={"X-Authenticated-Groups": "[operacion]"}
    )
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


def test_cambiar_plan_reenvia_al_administrador(api: ApiSociosFalsa) -> None:
    respuesta = cliente.put(
        "/socios/SOC-000001/plan", json={"plan": "prueba"}, headers=ADMIN_SOCIOS
    )
    assert respuesta.status_code == 200 and respuesta.json()["plan"] == "prueba"
    (enviada,) = api.solicitudes
    assert (enviada.method, enviada.url.path) == ("PUT", "/admin/socios/SOC-000001/plan")
    assert json.loads(enviada.content) == {"plan": "prueba"}
    assert enviada.headers["X-Authenticated-Sub"] == "sub-paula"


@pytest.mark.parametrize("cuerpo", [{"plan": "oro"}, {}, {"plan": "prueba", "cuota": 999999}])
def test_cambio_de_plan_invalido_no_llega_a_api_socios(
    api: ApiSociosFalsa, cuerpo: dict[str, Any]
) -> None:
    respuesta = cliente.put("/socios/SOC-000001/plan", json=cuerpo, headers=ADMIN_SOCIOS)
    assert respuesta.status_code == 422 and respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert not api.solicitudes


@pytest.mark.parametrize(
    "status, codigo, detalles",
    [
        (422, "PLAN_NO_DISPONIBLE", {"plan": "prueba"}),
        (409, "SOCIO_DESACTIVADO", {}),
    ],
)
def test_errores_del_cambio_de_plan_se_reenvian(
    api: ApiSociosFalsa, status: int, codigo: str, detalles: dict[str, Any]
) -> None:
    api.error_negocio = (
        status,
        {"codigo": codigo, "mensaje": "x", "correlationId": "c", "detalles": detalles},
    )
    respuesta = cliente.put(
        "/socios/SOC-000001/plan", json={"plan": "prueba"}, headers=ADMIN_SOCIOS
    )
    assert respuesta.status_code == status
    assert respuesta.json()["codigo"] == codigo and respuesta.json()["detalles"] == detalles


def test_consumo_solo_con_los_campos_publicados(api: ApiSociosFalsa) -> None:
    respuesta = cliente.get("/socios/SOC-000001/consumo", headers=ADMIN_SOCIOS)
    assert respuesta.status_code == 200
    assert respuesta.json() == CONSUMO
    assert "apiKeyId" not in respuesta.text and "key-que-no-debe-salir" not in respuesta.text
    assert api.solicitudes[0].url.path == "/admin/socios/SOC-000001/consumo"


def test_planes_no_se_confunde_con_un_identificador(api: ApiSociosFalsa) -> None:
    respuesta = cliente.get("/socios/planes", headers=ADMIN_SOCIOS)
    assert respuesta.status_code == 200
    assert [p["plan"] for p in respuesta.json()] == ["basico", "prueba"]
    assert cliente.get("/socios/SOC-000001/consumo", headers=ADMIN_SOCIOS).status_code == 200
    assert (
        cliente.get("/socios/otro/consumo", headers=ADMIN_SOCIOS).json()["codigo"]
        == "SOLICITUD_INVALIDA"
    )
