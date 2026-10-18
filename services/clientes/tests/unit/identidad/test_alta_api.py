"""API del alta y la verificación documental de clientes por un asesor (HU-W30)."""

import json
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from clientes.config import Settings
from clientes.main import crear_app
from fastapi.testclient import TestClient

ASESOR = {"X-Actor-Id": "asesor-1", "X-Actor-Rol": "ASESOR"}
CORRELACION = "corr-api-w30"
ALTA = {
    "tipoDocumento": "CC",
    "numeroDocumento": "1020456789",
    "nombreCompleto": "Natalia González Ruiz",
    "fechaNacimiento": "1990-04-18",
}

Manejador = Callable[[httpx.Request], httpx.Response]


def _veredicto(resultado: str = "APROBADO", motivo: str | None = None) -> Manejador:
    return lambda _: httpx.Response(
        200,
        json={
            "resultado": resultado,
            "motivo": motivo,
            "referencia": "kyc-ref-001",
            "proveedor": "KYC",
        },
    )


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


def _alta(cliente: TestClient, **cambios: Any) -> httpx.Response:
    return cliente.post("/v1/clientes", json={**ALTA, **cambios}, headers=ASESOR)


def _verificar(cliente: TestClient, cliente_id: str) -> httpx.Response:
    return cliente.post(
        f"/v1/clientes/{cliente_id}/identidad/verificacion-documental", headers=ASESOR
    )


def _error(respuesta: httpx.Response, estado: int, codigo: str) -> dict[str, Any]:
    assert respuesta.status_code == estado, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == codigo
    assert cuerpo["correlationId"] == CORRELACION
    return cuerpo


# --- Alta ---


def test_201_el_cliente_nace_pendiente(api: Any) -> None:
    cliente, recibidas = api(_veredicto())
    respuesta = _alta(cliente)
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    UUID(cuerpo.pop("clienteId"))
    assert cuerpo == {
        "estado": "PENDIENTE",
        "verificadoEn": None,
        "proveedor": None,
        "referencia": None,
    }
    assert recibidas == []  # crear al cliente no consulta al proveedor


def test_200_el_mismo_documento_no_duplica_al_cliente(api: Any, repositorio: Any) -> None:
    """AC4: se conserva una sola identidad activa."""
    cliente, _ = api(_veredicto())
    primero = _alta(cliente)
    segundo = _alta(cliente, nombreCompleto="Otro Nombre")
    assert (primero.status_code, segundo.status_code) == (201, 200)
    assert primero.json() == segundo.json()
    assert len(repositorio.clientes) == 1


def test_la_respuesta_no_devuelve_pii(api: Any) -> None:
    cliente, _ = api(_veredicto())
    texto = _alta(cliente).text
    for pii in ("1020456789", "Natalia", "1990-04-18"):
        assert pii not in texto


@pytest.mark.parametrize(
    "cambios",
    [
        {"tipoDocumento": "cc"},
        {"tipoDocumento": ""},
        {"numeroDocumento": "12"},
        {"numeroDocumento": "1020 456"},
        {"numeroDocumento": "1" * 21},
        {"nombreCompleto": ""},
        {"fechaNacimiento": "18/04/1990"},
        {"fechaNacimiento": 0},
        {"fechaNacimiento": None},
        {"fechaNacimiento": "2999-01-01"},
    ],
)
def test_422_alta_invalida(api: Any, repositorio: Any, cambios: dict[str, Any]) -> None:
    cliente, _ = api(_veredicto())
    assert _alta(cliente, **cambios).status_code == 422
    assert repositorio.clientes == {}


def test_401_y_403_alta_sin_asesor(api: Any) -> None:
    cliente, _ = api(_veredicto())
    _error(cliente.post("/v1/clientes", json=ALTA), 401, "ACTOR_REQUERIDO")
    titular = {"X-Actor-Id": "s", "X-Actor-Rol": "CLIENTE"}
    _error(cliente.post("/v1/clientes", json=ALTA, headers=titular), 403, "ROL_NO_AUTORIZADO")


# --- Verificación documental ---


def test_aprobado_pasa_a_verificado_con_fecha_y_referencia(api: Any) -> None:
    """AC1: VERIFICADO, con fecha y referencia de la validación."""
    cliente, recibidas = api(_veredicto("APROBADO"))
    cliente_id = _alta(cliente).json()["clienteId"]
    respuesta = _verificar(cliente, cliente_id)
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "VERIFICADO"
    assert cuerpo["verificadoEn"].endswith("Z")
    assert (cuerpo["proveedor"], cuerpo["referencia"]) == ("KYC", "kyc-ref-001")
    (request,) = recibidas
    assert request.url.path == "/v1/verificaciones/documento"
    assert request.headers["X-Correlation-Id"] == CORRELACION
    # El estado leído después coincide.
    assert cliente.get(f"/v1/clientes/{cliente_id}", headers=ASESOR).json() == cuerpo


def test_rechazado_no_revela_el_motivo_del_proveedor(api: Any) -> None:
    """AC2: la identidad no pudo verificarse, sin revelar reglas internas."""
    cliente, _ = api(_veredicto("RECHAZADO", "DATOS_NO_COINCIDEN"))
    cliente_id = _alta(cliente).json()["clienteId"]
    respuesta = _verificar(cliente, cliente_id)
    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "RECHAZADO"
    assert "DATOS_NO_COINCIDEN" not in respuesta.text
    assert "motivo" not in respuesta.json()


def test_la_verificacion_envia_los_datos_del_cliente_al_adaptador(api: Any) -> None:
    cliente, recibidas = api(_veredicto())
    _verificar(cliente, _alta(cliente).json()["clienteId"])
    cuerpo = json.loads(recibidas[0].content)
    assert {k: cuerpo[k] for k in ALTA} == ALTA
    assert "selfie" not in cuerpo


def test_confirmacion_repetida_no_vuelve_a_llamar_al_proveedor(api: Any) -> None:
    """AC4: procesa la confirmación de forma idempotente."""
    cliente, recibidas = api(_veredicto())
    cliente_id = _alta(cliente).json()["clienteId"]
    primera = _verificar(cliente, cliente_id)
    segunda = _verificar(cliente, cliente_id)
    assert primera.json() == segunda.json()
    assert len(recibidas) == 1


@pytest.mark.parametrize(
    ("manejador", "motivo"),
    [
        (lambda r: httpx.ReadTimeout("lento", request=r), "TIEMPO_AGOTADO"),
        (lambda r: httpx.ConnectError("caído", request=r), "ERROR"),
        (lambda _: httpx.Response(503, json={"codigo": "FUENTE_NO_DISPONIBLE"}), "ERROR"),
        (lambda _: httpx.Response(504, json={"codigo": "TIEMPO_AGOTADO"}), "TIEMPO_AGOTADO"),
        (lambda _: httpx.Response(200, text="<html>"), "ERROR"),
    ],
)
def test_falla_tecnica_responde_503_y_el_cliente_sigue_pendiente(
    api: Any, repositorio: Any, manejador: Manejador, motivo: str
) -> None:
    """AC3: no se convierte en rechazo; se puede reintentar."""
    cliente, _ = api(manejador)
    cliente_id = _alta(cliente).json()["clienteId"]
    cuerpo = _error(_verificar(cliente, cliente_id), 503, "DEPENDENCIA_NO_DISPONIBLE")
    assert cuerpo["detalles"] == {"motivo": motivo, "estado": "PENDIENTE"}
    assert cliente.get(f"/v1/clientes/{cliente_id}", headers=ASESOR).json()["estado"] == "PENDIENTE"


def test_404_cliente_inexistente(api: Any) -> None:
    cliente, recibidas = api(_veredicto())
    _error(_verificar(cliente, str(uuid4())), 404, "CLIENTE_NO_ENCONTRADO")
    _error(cliente.get(f"/v1/clientes/{uuid4()}", headers=ASESOR), 404, "CLIENTE_NO_ENCONTRADO")
    assert recibidas == []


def test_409_cliente_sin_documento(api: Any, repositorio: Any) -> None:
    cliente, recibidas = api(_veredicto())
    sin_documento = repositorio.asegurar("sub-1")
    _error(_verificar(cliente, str(sin_documento.id)), 409, "CLIENTE_SIN_DOCUMENTO")
    assert recibidas == []


def test_401_y_403_en_verificar_y_consultar(api: Any) -> None:
    cliente, _ = api(_veredicto())
    cliente_id = str(uuid4())
    titular = {"X-Actor-Id": "s", "X-Actor-Rol": "CLIENTE"}
    ruta = f"/v1/clientes/{cliente_id}/identidad/verificacion-documental"
    _error(cliente.post(ruta), 401, "ACTOR_REQUERIDO")
    _error(cliente.post(ruta, headers=titular), 403, "ROL_NO_AUTORIZADO")
    _error(cliente.get(f"/v1/clientes/{cliente_id}"), 401, "ACTOR_REQUERIDO")
    _error(cliente.get(f"/v1/clientes/{cliente_id}", headers=titular), 403, "ROL_NO_AUTORIZADO")


def test_la_ruta_actual_sigue_siendo_del_titular(api: Any) -> None:
    """`/v1/clientes/actual` no se confunde con `/{cliente_id}` del asesor."""
    cliente, _ = api(_veredicto())
    titular = {"X-Actor-Id": "sub-sin-cliente", "X-Actor-Rol": "CLIENTE"}
    _error(cliente.get("/v1/clientes/actual", headers=titular), 404, "CLIENTE_NO_ENCONTRADO")
