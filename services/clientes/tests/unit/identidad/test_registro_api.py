"""API del registro previo: verificación sin cuenta, token y vínculo en el primer ingreso."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from clientes.config import SECRETO_LOCAL, Settings
from clientes.main import crear_app
from fastapi.testclient import TestClient
from solventa_common import token_registro

REGISTRO = "00000000-0000-4000-8000-0000000000aa"
CORRELACION = "corr-registro-m06"
ACTOR = {"X-Actor-Id": "sub-nuevo-1", "X-Actor-Rol": "CLIENTE"}
CUERPO = {
    "correo": "Persona@Ejemplo.co",
    "selfie": "selfie-sintetica-aprobada",
    "pruebaDeVida": {"completado": True},
}
VERIFICAR = f"/v1/registros/{REGISTRO}/verificaciones"
VINCULAR = f"/v1/registros/{REGISTRO}/vinculacion"

Manejador = Callable[[httpx.Request], httpx.Response]


def _veredicto(resultado: str, motivo: str | None = None) -> Manejador:
    return lambda _: httpx.Response(200, json={"resultado": resultado, "motivo": motivo})


@pytest.fixture
def api(repositorio: Any) -> Iterator[Callable[[Manejador], TestClient]]:
    abiertos: list[TestClient] = []

    def crear(manejador: Manejador) -> TestClient:
        app = crear_app(
            Settings(), transport=httpx.MockTransport(manejador), repositorio=repositorio
        )
        cliente = TestClient(app, headers={"X-Correlation-Id": CORRELACION})
        cliente.__enter__()
        abiertos.append(cliente)
        return cliente

    yield crear
    for cliente in abiertos:
        cliente.__exit__(None, None, None)


def _error(respuesta: httpx.Response, estado: int, codigo: str) -> dict[str, Any]:
    assert respuesta.status_code == estado, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == codigo
    assert cuerpo["correlationId"] == CORRELACION
    return cuerpo


def _verificado(cliente: TestClient) -> dict[str, Any]:
    respuesta = cliente.post(VERIFICAR, json=CUERPO)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def test_200_verificado_devuelve_el_token_de_registro(api: Any) -> None:
    """AC1: la verificación exitosa entrega lo que habilita el SignUp de Cognito."""
    cliente = api(_veredicto("APROBADO"))

    cuerpo = _verificado(cliente)

    assert (cuerpo["estado"], cuerpo["resultado"]) == ("VERIFICADO", "APROBADO")
    UUID(cuerpo["clienteId"])
    claims = token_registro.verificar(
        SECRETO_LOCAL, cuerpo["tokenRegistro"], correo="persona@ejemplo.co", registro_id=REGISTRO
    )
    assert claims.cliente_id == cuerpo["clienteId"]
    expira = datetime.fromisoformat(cuerpo["expiraEn"])
    assert expira.tzinfo is not None and int(expira.timestamp()) == claims.expira_en
    assert expira > datetime.now(UTC)


def test_200_captura_invalida_no_entrega_token(api: Any) -> None:
    """AC2: se repite la captura; el cliente no queda rechazado ni habilitado."""
    cliente = api(_veredicto("CAPTURA_INVALIDA", "ILUMINACION_INSUFICIENTE"))

    cuerpo = cliente.post(VERIFICAR, json=CUERPO).json()

    assert cuerpo["estado"] == "PENDIENTE"
    assert cuerpo["motivo"] == "ILUMINACION_INSUFICIENTE"
    assert cuerpo["tokenRegistro"] is None and cuerpo["expiraEn"] is None


def test_200_rechazado_no_entrega_token(api: Any) -> None:
    cliente = api(_veredicto("RECHAZADO", "ROSTRO_NO_COINCIDE"))

    cuerpo = cliente.post(VERIFICAR, json=CUERPO).json()

    assert (cuerpo["estado"], cuerpo["tokenRegistro"]) == ("RECHAZADO", None)


@pytest.mark.parametrize(
    ("respuesta", "motivo"),
    [(httpx.Response(503), "ERROR"), (httpx.Response(504), "TIEMPO_AGOTADO")],
)
def test_503_la_verificacion_no_termino(api: Any, respuesta: httpx.Response, motivo: str) -> None:
    """AC3: error recuperable, sin token y sin rechazo."""
    cliente = api(lambda _: respuesta)

    cuerpo = _error(cliente.post(VERIFICAR, json=CUERPO), 503, "DEPENDENCIA_NO_DISPONIBLE")

    assert cuerpo["detalles"] == {"motivo": motivo}


def test_reintento_tras_interrupcion_no_crea_otro_cliente(api: Any, repositorio: Any) -> None:
    respuestas = iter([httpx.Response(503), httpx.Response(200, json={"resultado": "APROBADO"})])
    cliente = api(lambda _: next(respuestas))

    assert cliente.post(VERIFICAR, json=CUERPO).status_code == 503
    (pendiente,) = repositorio.clientes.values()
    exitoso = cliente.post(VERIFICAR, json=CUERPO)

    assert exitoso.status_code == 200
    assert len(repositorio.clientes) == 1
    assert exitoso.json()["clienteId"] == str(pendiente.id)


def test_409_el_registro_no_se_reusa_con_otro_correo(api: Any) -> None:
    cliente = api(_veredicto("APROBADO"))
    _verificado(cliente)

    otro = {**CUERPO, "correo": "otra@ejemplo.co"}
    _error(cliente.post(VERIFICAR, json=otro), 409, "REGISTRO_CORREO_DISTINTO")


def test_409_un_registro_con_cuenta_no_se_vuelve_a_verificar(api: Any) -> None:
    cliente = api(_veredicto("APROBADO"))
    token = _verificado(cliente)["tokenRegistro"]
    assert cliente.post(VINCULAR, json={"tokenRegistro": token}, headers=ACTOR).status_code == 200

    _error(cliente.post(VERIFICAR, json=CUERPO), 409, "REGISTRO_YA_VINCULADO")


@pytest.mark.parametrize(
    ("cambios", "campos"),
    [
        ({"correo": "no-es-correo"}, ["correo"]),
        ({"correo": ""}, ["correo"]),
        ({"selfie": ""}, ["selfie"]),
        ({"pruebaDeVida": "no-es-objeto"}, ["pruebaDeVida"]),
    ],
)
def test_422_solicitud_invalida(
    api: Any, repositorio: Any, cambios: dict[str, Any], campos: list[str]
) -> None:
    cliente = api(_veredicto("APROBADO"))

    respuesta = cliente.post(VERIFICAR, json={**CUERPO, **cambios})

    assert _error(respuesta, 422, "SOLICITUD_INVALIDA")["detalles"] == {"campos": campos}
    assert repositorio.clientes == {}


def test_422_registro_id_invalido(api: Any) -> None:
    cliente = api(_veredicto("APROBADO"))

    _error(
        cliente.post("/v1/registros/no-uuid/verificaciones", json=CUERPO), 422, "SOLICITUD_INVALIDA"
    )


def test_200_la_vinculacion_asocia_el_sub_y_resuelve_la_sesion(api: Any) -> None:
    """Tras el primer ingreso, `GET /actual` ya encuentra al cliente por su `sub`."""
    cliente = api(_veredicto("APROBADO"))
    verificado = _verificado(cliente)

    vinculado = cliente.post(
        VINCULAR, json={"tokenRegistro": verificado["tokenRegistro"]}, headers=ACTOR
    )

    assert vinculado.status_code == 200, vinculado.text
    assert vinculado.json()["clienteId"] == verificado["clienteId"]
    assert vinculado.json()["estado"] == "VERIFICADO"
    actual = cliente.get("/v1/clientes/actual", headers=ACTOR)
    assert actual.status_code == 200
    assert actual.json()["clienteId"] == verificado["clienteId"]


def test_la_vinculacion_es_idempotente(api: Any) -> None:
    cliente = api(_veredicto("APROBADO"))
    cuerpo = {"tokenRegistro": _verificado(cliente)["tokenRegistro"]}

    primero = cliente.post(VINCULAR, json=cuerpo, headers=ACTOR)
    segundo = cliente.post(VINCULAR, json=cuerpo, headers=ACTOR)

    assert primero.status_code == segundo.status_code == 200
    assert primero.json() == segundo.json()


def test_409_otro_titular_no_se_apropia_del_registro(api: Any) -> None:
    cliente = api(_veredicto("APROBADO"))
    cuerpo = {"tokenRegistro": _verificado(cliente)["tokenRegistro"]}
    cliente.post(VINCULAR, json=cuerpo, headers=ACTOR)

    otro = {"X-Actor-Id": "sub-intruso", "X-Actor-Rol": "CLIENTE"}
    _error(cliente.post(VINCULAR, json=cuerpo, headers=otro), 409, "REGISTRO_YA_VINCULADO")


@pytest.mark.parametrize(
    ("token", "motivo"),
    [("basura", "FORMATO"), ("v1.a.b", "FIRMA")],
)
def test_403_token_de_registro_invalido(api: Any, token: str, motivo: str) -> None:
    cliente = api(_veredicto("APROBADO"))
    _verificado(cliente)

    cuerpo = _error(
        cliente.post(VINCULAR, json={"tokenRegistro": token}, headers=ACTOR),
        403,
        "TOKEN_REGISTRO_INVALIDO",
    )

    assert cuerpo["detalles"] == {"motivo": motivo}


def test_403_el_token_de_otro_registro_no_sirve(api: Any) -> None:
    cliente = api(_veredicto("APROBADO"))
    token = _verificado(cliente)["tokenRegistro"]

    otro = "/v1/registros/00000000-0000-4000-8000-0000000000bb/vinculacion"
    cuerpo = _error(
        cliente.post(otro, json={"tokenRegistro": token}, headers=ACTOR),
        403,
        "TOKEN_REGISTRO_INVALIDO",
    )

    assert cuerpo["detalles"] == {"motivo": "REGISTRO"}


@pytest.mark.parametrize(
    ("headers", "estado", "codigo"),
    [
        ({}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": "sub-1"}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": "sub-1", "X-Actor-Rol": "ASESOR"}, 403, "ROL_NO_AUTORIZADO"),
    ],
)
def test_la_vinculacion_exige_el_actor_antes_de_validar_el_cuerpo(
    api: Any, headers: dict[str, str], estado: int, codigo: str
) -> None:
    cliente = api(_veredicto("APROBADO"))

    _error(cliente.post(VINCULAR, json={}, headers=headers), estado, codigo)


@pytest.mark.parametrize("cuerpo", [{}, {"tokenRegistro": ""}, {"tokenRegistro": "x" * 2049}])
def test_422_la_vinculacion_exige_el_token(api: Any, cuerpo: dict[str, Any]) -> None:
    cliente = api(_veredicto("APROBADO"))

    _error(cliente.post(VINCULAR, json=cuerpo, headers=ACTOR), 422, "SOLICITUD_INVALIDA")
