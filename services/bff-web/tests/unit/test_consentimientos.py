"""HU-W31 (PR 3.5): lectura de consentimientos del cliente para el asesor, con `clientes` simulado.

El BFF solo transmite: no recalcula el estado, no cachea y falla cerrado ante cualquier respuesta
de `clientes` fuera de su contrato.
"""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import consentimientos as ruta
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.application.consentimientos import Consentimientos
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
ASESOR = {
    "X-Authenticated-Groups": "[asesores]",
    "X-Authenticated-Issuer": BACKOFFICE,
    "X-Authenticated-Sub": "sub-asesora",
    "X-Correlation-Id": "corr-bff-w31",
}
CLIENTE_ID = "00000000-0000-4000-8000-000000000031"
CONSENTIMIENTO_ID = "00000000-0000-4000-9000-000000000031"
LISTA = f"/v1/clientes/{CLIENTE_ID}/consentimientos"
HISTORIAL = f"{LISTA}/{CONSENTIMIENTO_ID}/historial"

ACTIVO = {
    "consentimientoId": CONSENTIMIENTO_ID,
    "estado": "ACTIVO",
    "proposito": "PERFILAMIENTO_PRECIO",
    "alcances": ["FA_PRODUCTOS_VIGENTES", "DA_REGISTROS_PUBLICOS"],
    "versionTexto": "2026-10-01",
    "idioma": "es-CO",
    "otorgadoEn": "2026-10-01T15:00:00Z",
    "vigenteHasta": "2027-10-01T15:00:00Z",
    "revocadoEn": None,
}
REVOCADO = {
    **ACTIVO,
    "consentimientoId": "00000000-0000-4000-9000-000000000032",
    "estado": "REVOCADO",
    "revocadoEn": "2026-10-05T10:30:00Z",
}
VENCIDO = {
    **ACTIVO,
    "consentimientoId": "00000000-0000-4000-9000-000000000033",
    "estado": "VENCIDO",
    "otorgadoEn": "2025-04-01T15:00:00Z",
    "vigenteHasta": "2025-10-01T15:00:00Z",
}
HISTORIAL_REVOCADO = {
    "consentimientoId": CONSENTIMIENTO_ID,
    "estado": "REVOCADO",
    "hitos": [
        {"tipo": "OTORGADO", "ocurridoEn": "2026-10-01T15:00:00Z"},
        {"tipo": "REVOCADO", "ocurridoEn": "2026-10-05T10:30:00Z"},
    ],
}


def _b2(estado: int, codigo: str, detalles: dict[str, Any] | None = None) -> httpx.Response:
    cuerpo = {"codigo": codigo, "mensaje": "m", "correlationId": "c", "detalles": detalles or {}}
    return httpx.Response(estado, json=cuerpo)


class ClientesFalso:
    def __init__(self) -> None:
        self.solicitudes: list[httpx.Request] = []
        self.respuestas: list[httpx.Response | Exception] = [httpx.Response(200, json=[ACTIVO])]

    @property
    def respuesta(self) -> httpx.Response | Exception:
        return self.respuestas[-1]

    @respuesta.setter
    def respuesta(self, valor: httpx.Response | Exception) -> None:
        self.respuestas = [valor]

    def __call__(self, solicitud: httpx.Request) -> httpx.Response:
        self.solicitudes.append(solicitud)
        # Cada llamada consume la siguiente respuesta; la última se repite.
        respuesta = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


@pytest.fixture
def clientes(monkeypatch: pytest.MonkeyPatch) -> Iterator[ClientesFalso]:
    falso = ClientesFalso()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cliente = ClientesCliente("http://clientes:8080", 5000, httpx.MockTransport(falso))
    app.dependency_overrides[ruta.consentimientos] = lambda: Consentimientos(cliente)
    yield falso
    app.dependency_overrides.clear()


def _get(url: str, encabezados: dict[str, str] | None = None) -> httpx.Response:
    return TestClient(app).get(url, headers=ASESOR if encabezados is None else encabezados)


# --- Autorización -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "encabezados",
    [
        {"X-Authenticated-Issuer": BACKOFFICE, "X-Authenticated-Sub": "s"},
        {
            "X-Authenticated-Groups": "[operacion]",
            "X-Authenticated-Issuer": BACKOFFICE,
            "X-Authenticated-Sub": "s",
        },
        {
            "X-Authenticated-Groups": "[asesores]",
            "X-Authenticated-Issuer": "https://otro",
            "X-Authenticated-Sub": "s",
        },
    ],
    ids=["sin-grupo", "otro-grupo", "otro-emisor"],
)
def test_solo_asesores_del_backoffice(clientes: ClientesFalso, encabezados: dict[str, str]) -> None:
    for url in (LISTA, HISTORIAL):
        respuesta = _get(url, encabezados)
        assert respuesta.status_code == 403
        assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"
        assert set(respuesta.json()) == {"codigo", "mensaje", "correlationId", "detalles"}
    assert clientes.solicitudes == []


def test_sin_usuario_autenticado_es_401(clientes: ClientesFalso) -> None:
    sin_sub = {k: v for k, v in ASESOR.items() if k != "X-Authenticated-Sub"}
    for url in (LISTA, HISTORIAL):
        respuesta = _get(url, sin_sub)
        assert (respuesta.status_code, respuesta.json()["codigo"]) == (401, "ACTOR_REQUERIDO")
    assert clientes.solicitudes == []


@pytest.mark.parametrize(
    "url",
    [
        "/v1/clientes/no-es-uuid/consentimientos",
        f"/v1/clientes/{CLIENTE_ID}/consentimientos/no-es-uuid/historial",
    ],
    ids=["clienteId", "consentimientoId"],
)
def test_identificador_invalido_es_422_sin_llamar_a_clientes(
    clientes: ClientesFalso, url: str
) -> None:
    respuesta = _get(url)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert clientes.solicitudes == []


def test_no_hay_rutas_de_escritura(clientes: ClientesFalso) -> None:
    """ADR 0002: el asesor no registra ni revoca."""
    cliente = TestClient(app)
    for metodo, url in (
        ("post", LISTA),
        ("put", LISTA),
        ("delete", f"{LISTA}/{CONSENTIMIENTO_ID}"),
        ("post", f"{LISTA}/{CONSENTIMIENTO_ID}/revocacion"),
        ("post", HISTORIAL),
    ):
        assert cliente.request(metodo, url, headers=ASESOR).status_code in (404, 405), url
    assert clientes.solicitudes == []


# --- Transmisión --------------------------------------------------------------------------


def test_lista_reenvia_con_el_rol_asesor_y_devuelve_lo_mismo(clientes: ClientesFalso) -> None:
    clientes.respuesta = httpx.Response(200, json=[ACTIVO, REVOCADO, VENCIDO])

    respuesta = _get(LISTA)

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == [ACTIVO, REVOCADO, VENCIDO]
    (solicitud,) = clientes.solicitudes
    assert (solicitud.method, solicitud.url.path) == ("GET", LISTA)
    assert solicitud.headers["X-Actor-Id"] == "sub-asesora"
    assert solicitud.headers["X-Actor-Rol"] == "ASESOR"
    assert solicitud.headers["X-Correlation-Id"] == "corr-bff-w31"
    assert solicitud.content == b""


def test_lista_vacia(clientes: ClientesFalso) -> None:
    clientes.respuesta = httpx.Response(200, json=[])
    respuesta = _get(LISTA)
    assert (respuesta.status_code, respuesta.json()) == (200, [])


def test_historial_reenvia_y_devuelve_lo_mismo(clientes: ClientesFalso) -> None:
    clientes.respuesta = httpx.Response(200, json=HISTORIAL_REVOCADO)

    respuesta = _get(HISTORIAL)

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == HISTORIAL_REVOCADO
    (solicitud,) = clientes.solicitudes
    assert (solicitud.method, solicitud.url.path) == ("GET", HISTORIAL)
    assert (solicitud.headers["X-Actor-Id"], solicitud.headers["X-Actor-Rol"]) == (
        "sub-asesora",
        "ASESOR",
    )


def test_no_recalcula_el_estado(clientes: ClientesFalso) -> None:
    """El estado es el que derivó `clientes` con el reloj de su base (ADR 0010): un ACTIVO con
    `vigenteHasta` ya pasado según el reloj del BFF se transmite tal cual."""
    activo_vencido_aqui = {**ACTIVO, "vigenteHasta": "2020-01-01T00:00:00Z"}
    clientes.respuesta = httpx.Response(200, json=[activo_vencido_aqui])
    assert _get(LISTA).json() == [activo_vencido_aqui]


def test_no_cachea(clientes: ClientesFalso) -> None:
    """ADR 0011: cada consulta llega a `clientes`; una revocación se ve en la siguiente."""
    revocado = {**ACTIVO, "estado": "REVOCADO", "revocadoEn": "2026-10-18T12:00:00Z"}
    clientes.respuestas = [httpx.Response(200, json=[ACTIVO]), httpx.Response(200, json=[revocado])]

    assert _get(LISTA).json() == [ACTIVO]
    assert _get(LISTA).json() == [revocado]
    assert len(clientes.solicitudes) == 2


def _claves(valor: Any, prefijo: str = "") -> set[str]:
    """Rutas de todas las claves, también las que valen null; los ítems de lista por índice."""
    if isinstance(valor, dict):
        return {f"{prefijo}.{k}" for k in valor} | {
            r for k, v in valor.items() for r in _claves(v, f"{prefijo}.{k}")
        }
    if isinstance(valor, list):
        return {r for i, v in enumerate(valor) for r in _claves(v, f"{prefijo}[{i}]")}
    return set()


def test_las_rutas_conservan_exactamente_las_claves_de_clientes(clientes: ClientesFalso) -> None:
    """`response_model_exclude_unset` no quita claves con null (`revocadoEn`) ni añade otras."""
    historial_activo = {
        "consentimientoId": CONSENTIMIENTO_ID,
        "estado": "ACTIVO",
        "hitos": [{"tipo": "OTORGADO", "ocurridoEn": "2026-10-01T15:00:00Z"}],
    }
    for url, enviado in (
        (LISTA, [ACTIVO, REVOCADO, VENCIDO]),
        (LISTA, []),
        (HISTORIAL, HISTORIAL_REVOCADO),
        (HISTORIAL, historial_activo),
    ):
        clientes.respuesta = httpx.Response(200, json=enviado)
        respuesta = _get(url)
        assert respuesta.status_code == 200, url
        assert _claves(respuesta.json()) == _claves(enviado), url
        assert respuesta.json() == enviado, url


def test_un_campo_que_el_contrato_no_conoce_no_se_transmite(clientes: ClientesFalso) -> None:
    """Minimización: solo salen las claves del DTO (como `motivo` en HU-W30)."""
    clientes.respuesta = httpx.Response(200, json=[{**ACTIVO, "sub": "sub-del-titular"}])
    respuesta = _get(LISTA)
    assert respuesta.json() == [ACTIVO]
    assert "sub-del-titular" not in respuesta.text


# --- Errores de clientes ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "estado", "codigo"),
    [
        (LISTA, 404, "CLIENTE_NO_ENCONTRADO"),
        (HISTORIAL, 404, "CONSENTIMIENTO_NO_ENCONTRADO"),
        (LISTA, 401, "ACTOR_REQUERIDO"),
        (HISTORIAL, 401, "ACTOR_REQUERIDO"),
        (LISTA, 403, "ROL_NO_AUTORIZADO"),
        (HISTORIAL, 403, "ROL_NO_AUTORIZADO"),
        (LISTA, 422, "SOLICITUD_INVALIDA"),
        (HISTORIAL, 422, "SOLICITUD_INVALIDA"),
    ],
)
def test_errores_del_contrato_pasan_con_su_estado(
    clientes: ClientesFalso, url: str, estado: int, codigo: str
) -> None:
    detalles = {"campos": ["clienteId"]} if estado == 422 else {}
    clientes.respuesta = _b2(estado, codigo, detalles)

    respuesta = _get(url)

    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
    assert respuesta.json()["detalles"] == detalles
    assert respuesta.json()["correlationId"] == "corr-bff-w31"


FUERA_DE_CONTRATO = [
    (LISTA, httpx.ReadTimeout("lento")),
    (LISTA, httpx.ConnectError("caído")),
    (HISTORIAL, httpx.ReadTimeout("lento")),
    (LISTA, httpx.Response(500)),
    (LISTA, httpx.Response(502, json={"detail": "x"})),
    (HISTORIAL, httpx.Response(504, json={"codigo": "TIEMPO_AGOTADO"})),
    (LISTA, _b2(503, "DEPENDENCIA_NO_DISPONIBLE", {"motivo": "ERROR"})),
    # `clientes` anterior a la 3.3: la ruta no existe.
    (LISTA, _b2(404, "NO_ENCONTRADO")),
    (HISTORIAL, httpx.Response(404, json={"detail": "Not Found"})),
    # Un 404 que la ruta no promete.
    (LISTA, _b2(404, "CONSENTIMIENTO_NO_ENCONTRADO")),
    (HISTORIAL, _b2(404, "CLIENTE_NO_ENCONTRADO")),
    (LISTA, _b2(409, "CLIENTE_SIN_DOCUMENTO")),
    (LISTA, httpx.Response(200, text="<html>")),
    (LISTA, httpx.Response(200, json={"items": [ACTIVO]})),
    (LISTA, httpx.Response(200, json=[{**ACTIVO, "estado": "SUSPENDIDO"}])),
    (LISTA, httpx.Response(200, json=[{k: v for k, v in ACTIVO.items() if k != "revocadoEn"}])),
    (LISTA, httpx.Response(200, json=[{**ACTIVO, "consentimientoId": "x"}])),
    (HISTORIAL, httpx.Response(200, json=[HISTORIAL_REVOCADO])),
    (HISTORIAL, httpx.Response(200, json={**HISTORIAL_REVOCADO, "hitos": [{"tipo": "PAUSADO"}]})),
]


@pytest.mark.parametrize(
    ("url", "falla"),
    FUERA_DE_CONTRATO,
    ids=[
        "timeout",
        "conexion",
        "timeout-historial",
        "500",
        "502-sin-b2",
        "504",
        "503-dependencia",
        "ruta-inexistente",
        "ruta-inexistente-historial",
        "404-ajeno-lista",
        "404-ajeno-historial",
        "409",
        "no-json",
        "objeto-en-vez-de-lista",
        "estado-desconocido",
        "falta-un-campo",
        "id-invalido",
        "lista-en-vez-de-historial",
        "hito-desconocido",
    ],
)
def test_lo_que_no_promete_el_contrato_es_503_sin_detalles(
    clientes: ClientesFalso, url: str, falla: httpx.Response | Exception
) -> None:
    """Falla cerrado: nunca se muestra un consentimiento que no se pudo leer bien."""
    clientes.respuesta = falla

    respuesta = _get(url)

    assert respuesta.status_code == 503, respuesta.text
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {}
    assert respuesta.json()["correlationId"] == "corr-bff-w31"
    assert "clientes:8080" not in respuesta.text
    assert "ACTIVO" not in respuesta.text


def test_el_cliente_real_se_construye_con_la_configuracion() -> None:
    ruta.consentimientos.cache_clear()
    servicio = ruta.consentimientos()
    assert servicio.clientes.url == settings.clientes_url.rstrip("/")
    assert servicio.clientes.timeout == settings.clientes_timeout_ms / 1000
    assert ruta.consentimientos() is servicio
    ruta.consentimientos.cache_clear()
