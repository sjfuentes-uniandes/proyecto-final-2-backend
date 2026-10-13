import json
from collections.abc import Callable
from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel
from solventa_common import correlation
from solventa_common.errors import (
    ErrorNegocio,
    RutaB2,
    campos_invalidos,
    cuerpo_error,
    registrar_manejadores,
)
from solventa_common.settings import ServiceSettings
from solventa_common.telemetry import instrumentar

CORRELACION = "corr-errores-1"
SECRETO = "valor-que-no-debe-salir-123"


class Cuerpo(BaseModel):
    nombre: str
    edad: int


def _prohibir() -> None:
    raise HTTPException(status_code=403, detail="solo administradores")


def _app() -> FastAPI:
    app = instrumentar(FastAPI(), ServiceSettings(service_name="prueba-errores"))
    registrar_manejadores(app)

    @app.get("/negocio")
    async def negocio() -> None:
        raise ErrorNegocio("CLIENTE_NO_ENCONTRADO", "Cliente no encontrado", 404, {"recurso": "x"})

    @app.post("/validacion")
    async def validacion(cuerpo: Cuerpo, limite: Annotated[int, Query()] = 1) -> dict[str, str]:
        return {"nombre": cuerpo.nombre}

    @app.get("/http")
    async def http() -> None:
        raise HTTPException(status_code=409, detail="conflicto")

    @app.get("/fallo")
    async def fallo() -> None:
        raise RuntimeError(SECRETO)

    b2 = APIRouter(prefix="/b2", route_class=RutaB2)

    @b2.get("/negocio")
    async def b2_negocio() -> None:
        raise ErrorNegocio("PERFIL_NO_ENCONTRADO", "Perfil no encontrado", 404)

    @b2.post("/validacion")
    async def b2_validacion(cuerpo: Cuerpo) -> dict[str, str]:
        return {"nombre": cuerpo.nombre}

    @b2.get("/prohibido", dependencies=[Depends(_prohibir)])
    async def b2_prohibido() -> None:
        return None

    @b2.get("/codigo/{estado}")
    async def b2_codigo(estado: int) -> None:
        raise HTTPException(status_code=estado, detail="x")

    app.include_router(b2)
    return app


def _app_sin_manejadores() -> FastAPI:
    """App que conserva {"detail"} en sus rutas propias y usa RutaB2 en un router."""
    app = FastAPI()

    @app.get("/w27")
    async def w27() -> None:
        raise HTTPException(status_code=403, detail="solo administradores")

    b2 = APIRouter(route_class=RutaB2)

    @b2.get("/nuevo", dependencies=[Depends(_prohibir)])
    async def nuevo() -> None:
        return None

    app.include_router(b2)
    return app


@pytest.fixture
def cliente() -> TestClient:
    return TestClient(
        _app(), raise_server_exceptions=False, headers={"X-Correlation-Id": CORRELACION}
    )


def _forma_b2(cuerpo: dict) -> None:
    assert set(cuerpo) == {"codigo", "mensaje", "correlationId", "detalles"}
    assert cuerpo["correlationId"] == CORRELACION


def test_error_de_negocio(cliente: TestClient) -> None:
    respuesta = cliente.get("/negocio")
    assert respuesta.status_code == 404
    _forma_b2(respuesta.json())
    assert respuesta.json()["codigo"] == "CLIENTE_NO_ENCONTRADO"
    assert respuesta.json()["detalles"] == {"recurso": "x"}


def test_validacion_solo_rutas_de_campos_sin_valores(cliente: TestClient) -> None:
    respuesta = cliente.post("/validacion?limite=abc", json={"nombre": SECRETO, "edad": SECRETO})
    assert respuesta.status_code == 422
    cuerpo = respuesta.json()
    _forma_b2(cuerpo)
    assert cuerpo["codigo"] == "SOLICITUD_INVALIDA"
    assert cuerpo["detalles"] == {"campos": ["limite", "edad"]}
    assert SECRETO not in respuesta.text


def test_validacion_sin_cuerpo(cliente: TestClient) -> None:
    respuesta = cliente.post("/validacion")
    assert respuesta.status_code == 422
    assert respuesta.json()["detalles"] == {"campos": ["body"]}


def test_campos_invalidos_sin_duplicados() -> None:
    errores = [
        {"loc": ("body", "a", 0)},
        {"loc": ("body", "a", 0)},
        {"loc": ("header", "x-deadline-ms")},
    ]
    assert campos_invalidos(errores) == ["a.0", "x-deadline-ms"]


@pytest.mark.parametrize(
    ("ruta", "metodo", "estado", "codigo"),
    [
        ("/no-existe", "get", 404, "NO_ENCONTRADO"),
        ("/negocio", "delete", 405, "METODO_NO_PERMITIDO"),
        ("/http", "get", 409, "ERROR_HTTP"),
    ],
)
def test_excepciones_http(
    cliente: TestClient, ruta: str, metodo: str, estado: int, codigo: str
) -> None:
    respuesta = getattr(cliente, metodo)(ruta)
    assert respuesta.status_code == estado
    _forma_b2(respuesta.json())
    assert respuesta.json()["codigo"] == codigo


def test_error_inesperado_solo_registra_el_tipo(
    cliente: TestClient, capturar_logs: Callable[..., object]
) -> None:
    salida = capturar_logs("prueba-errores")
    respuesta = cliente.get("/fallo")
    assert respuesta.status_code == 500
    _forma_b2(respuesta.json())
    assert respuesta.json()["codigo"] == "ERROR_INTERNO"
    assert respuesta.json()["mensaje"] == "Error interno"
    assert SECRETO not in respuesta.text
    lineas = [json.loads(linea) for linea in salida.getvalue().splitlines()]  # type: ignore[attr-defined]
    errores = [linea for linea in lineas if linea["logger"] == "solventa.errores"]
    assert errores and errores[0]["error_type"] == "RuntimeError"
    assert SECRETO not in salida.getvalue()  # type: ignore[attr-defined]


def test_ruta_b2_negocio_y_validacion(cliente: TestClient) -> None:
    respuesta = cliente.get("/b2/negocio")
    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "PERFIL_NO_ENCONTRADO"
    respuesta = cliente.post("/b2/validacion", json={"nombre": "x"})
    assert respuesta.status_code == 422
    assert respuesta.json()["detalles"] == {"campos": ["edad"]}


def test_ruta_b2_traduce_la_excepcion_de_una_dependencia(cliente: TestClient) -> None:
    respuesta = cliente.get("/b2/prohibido")
    assert respuesta.status_code == 403
    _forma_b2(respuesta.json())
    assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"


@pytest.mark.parametrize(
    ("estado", "codigo"),
    [
        (400, "SOLICITUD_INVALIDA"),
        (401, "NO_AUTENTICADO"),
        (404, "NO_ENCONTRADO"),
        (418, "ERROR_HTTP"),
    ],
)
def test_ruta_b2_codigos_http(cliente: TestClient, estado: int, codigo: str) -> None:
    respuesta = cliente.get(f"/b2/codigo/{estado}")
    assert respuesta.status_code == estado
    assert respuesta.json()["codigo"] == codigo


def test_ruta_b2_no_afecta_otro_router() -> None:
    cliente = TestClient(_app_sin_manejadores())
    assert cliente.get("/w27").json() == {"detail": "solo administradores"}
    respuesta = cliente.get("/nuevo")
    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"


def test_cuerpo_error_usa_el_correlation_id_del_contexto() -> None:
    token = correlation.establecer("ctx-1")
    try:
        assert cuerpo_error("X", "y") == {
            "codigo": "X",
            "mensaje": "y",
            "correlationId": "ctx-1",
            "detalles": {},
        }
    finally:
        correlation.restablecer(token)
