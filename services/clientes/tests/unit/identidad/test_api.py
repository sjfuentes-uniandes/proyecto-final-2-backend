from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from clientes.config import Settings
from clientes.main import crear_app
from fastapi.testclient import TestClient

SUB = "sub-cliente-1"
ACTOR = {"X-Actor-Id": SUB, "X-Actor-Rol": "CLIENTE"}
CORRELACION = "corr-api-m06"
SELFIE = {"selfie": "selfie-sintetica-aprobada", "pruebaDeVida": {"completado": True}}

Manejador = Callable[[httpx.Request], httpx.Response]


def _veredicto(resultado: str, motivo: str | None = None) -> Manejador:
    return lambda _: httpx.Response(200, json={"resultado": resultado, "motivo": motivo})


@pytest.fixture
def api(
    repositorio: Any,
) -> Iterator[Callable[[Manejador], tuple[TestClient, list[httpx.Request]]]]:
    abiertos: list[TestClient] = []

    def crear(manejador: Manejador) -> tuple[TestClient, list[httpx.Request]]:
        recibidas: list[httpx.Request] = []

        async def registrar(request: httpx.Request) -> httpx.Response:
            recibidas.append(request)
            respuesta = manejador(request)
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

        app = crear_app(
            Settings(), transport=httpx.MockTransport(registrar), repositorio=repositorio
        )
        cliente = TestClient(app, headers={"X-Correlation-Id": CORRELACION})
        cliente.__enter__()
        abiertos.append(cliente)
        return cliente, recibidas

    yield crear
    for cliente in abiertos:
        cliente.__exit__(None, None, None)


def _error(respuesta: httpx.Response, estado: int, codigo: str) -> dict[str, Any]:
    assert respuesta.status_code == estado, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == codigo
    assert cuerpo["correlationId"] == CORRELACION
    return cuerpo


def test_200_verificado(api: Any) -> None:
    cliente, recibidas = api(_veredicto("APROBADO"))
    respuesta = cliente.post(
        "/v1/clientes/actual/identidad/verificaciones", json=SELFIE, headers=ACTOR
    )
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["verificadoEn"].endswith("Z")
    cuerpo.pop("verificadoEn")
    cliente_id = cuerpo.pop("clienteId")
    UUID(cliente_id)
    assert cuerpo == {"estado": "VERIFICADO", "resultado": "APROBADO", "motivo": None}
    (request,) = recibidas
    assert request.url.path == "/v1/verificaciones"
    assert request.headers["X-Correlation-Id"] == CORRELACION


def test_200_captura_invalida_no_rechaza(api: Any) -> None:
    """AC2: 200 con el cliente aún pendiente y el motivo para repetir la captura."""
    cliente, _ = api(_veredicto("CAPTURA_INVALIDA", "ILUMINACION_INSUFICIENTE"))
    cuerpo = cliente.post(
        "/v1/clientes/actual/identidad/verificaciones", json=SELFIE, headers=ACTOR
    ).json()
    assert cuerpo["estado"] == "PENDIENTE"
    assert cuerpo["resultado"] == "CAPTURA_INVALIDA"
    assert cuerpo["motivo"] == "ILUMINACION_INSUFICIENTE"
    assert cuerpo["verificadoEn"] is None


def test_200_rechazado(api: Any) -> None:
    cliente, _ = api(_veredicto("RECHAZADO", "ROSTRO_NO_COINCIDE"))
    cuerpo = cliente.post(
        "/v1/clientes/actual/identidad/verificaciones", json=SELFIE, headers=ACTOR
    ).json()
    assert (cuerpo["estado"], cuerpo["resultado"]) == ("RECHAZADO", "RECHAZADO")


@pytest.mark.parametrize(
    ("respuesta", "motivo"),
    [
        (httpx.Response(503, json={"codigo": "FUENTE_NO_DISPONIBLE"}), "ERROR"),
        (httpx.Response(504, json={"codigo": "TIEMPO_AGOTADO"}), "TIEMPO_AGOTADO"),
    ],
)
def test_503_la_verificacion_no_termino(api: Any, respuesta: httpx.Response, motivo: str) -> None:
    """AC3: error recuperable; el cliente queda pendiente y el reintento reutiliza el mismo."""
    cliente, _ = api(lambda _: respuesta)
    cuerpo = _error(
        cliente.post("/v1/clientes/actual/identidad/verificaciones", json=SELFIE, headers=ACTOR),
        503,
        "DEPENDENCIA_NO_DISPONIBLE",
    )
    assert cuerpo["detalles"] == {"motivo": motivo}


def test_reintento_tras_interrupcion_no_crea_otro_cliente(api: Any, repositorio: Any) -> None:
    estados = iter(
        [httpx.Response(503), httpx.Response(200, json={"resultado": "APROBADO", "motivo": None})]
    )
    cliente, _ = api(lambda _: next(estados))
    fallido = cliente.post(
        "/v1/clientes/actual/identidad/verificaciones", json=SELFIE, headers=ACTOR
    )
    assert fallido.status_code == 503
    (pendiente,) = repositorio.clientes.values()
    exitoso = cliente.post(
        "/v1/clientes/actual/identidad/verificaciones", json=SELFIE, headers=ACTOR
    )
    assert exitoso.status_code == 200
    assert len(repositorio.clientes) == 1
    assert exitoso.json()["clienteId"] == str(pendiente.id)


def test_cliente_actual_y_identidad_tras_verificar(api: Any) -> None:
    cliente, _ = api(_veredicto("APROBADO"))
    cliente_id = cliente.post(
        "/v1/clientes/actual/identidad/verificaciones", json=SELFIE, headers=ACTOR
    ).json()["clienteId"]
    actual = cliente.get("/v1/clientes/actual", headers=ACTOR)
    assert actual.status_code == 200
    assert actual.json()["clienteId"] == cliente_id
    identidad = cliente.get(f"/v1/clientes/{cliente_id}/identidad")
    assert identidad.status_code == 200
    assert identidad.json()["estado"] == "VERIFICADO"
    assert identidad.json()["verificadoEn"].endswith("Z")


def test_404_cliente_actual_sin_verificacion_previa(api: Any, repositorio: Any) -> None:
    cliente, _ = api(_veredicto("APROBADO"))
    _error(cliente.get("/v1/clientes/actual", headers=ACTOR), 404, "CLIENTE_NO_ENCONTRADO")
    assert repositorio.clientes == {}


def test_404_identidad_de_cliente_inexistente(api: Any) -> None:
    cliente, _ = api(_veredicto("APROBADO"))
    respuesta = cliente.get("/v1/clientes/00000000-0000-4000-8000-0000000000ff/identidad")
    _error(respuesta, 404, "CLIENTE_NO_ENCONTRADO")


@pytest.mark.parametrize(
    ("headers", "estado", "codigo"),
    [
        ({}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": SUB}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": SUB, "X-Actor-Rol": "ASESOR"}, 403, "ROL_NO_AUTORIZADO"),
    ],
)
def test_actor_requerido_antes_de_validar_el_cuerpo(
    api: Any, headers: dict[str, str], estado: int, codigo: str
) -> None:
    cliente, recibidas = api(_veredicto("APROBADO"))
    _error(
        cliente.post("/v1/clientes/actual/identidad/verificaciones", json={}, headers=headers),
        estado,
        codigo,
    )
    _error(cliente.get("/v1/clientes/actual", headers=headers), estado, codigo)
    assert recibidas == []


@pytest.mark.parametrize(
    ("cuerpo", "campos"),
    [
        ({"pruebaDeVida": {}}, ["selfie"]),
        ({"selfie": "", "pruebaDeVida": {}}, ["selfie"]),
        ({"selfie": "x"}, ["pruebaDeVida"]),
    ],
)
def test_422_solicitud_invalida(
    api: Any, repositorio: Any, cuerpo: dict[str, Any], campos: list[str]
) -> None:
    cliente, recibidas = api(_veredicto("APROBADO"))
    respuesta = cliente.post(
        "/v1/clientes/actual/identidad/verificaciones", json=cuerpo, headers=ACTOR
    )
    assert _error(respuesta, 422, "SOLICITUD_INVALIDA")["detalles"] == {"campos": campos}
    assert recibidas == []
    assert repositorio.clientes == {}


def test_422_cliente_id_invalido(api: Any) -> None:
    cliente, _ = api(_veredicto("APROBADO"))
    _error(cliente.get("/v1/clientes/no-uuid/identidad"), 422, "SOLICITUD_INVALIDA")


def test_health_sin_dependencias(api: Any) -> None:
    cliente, recibidas = api(_veredicto("APROBADO"))
    assert cliente.get("/health").json() == {"status": "ok"}
    assert recibidas == []
