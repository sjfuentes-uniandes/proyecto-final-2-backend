"""POST /v1/registro/vinculacion y /validaciones (HU-M06): primer ingreso y simulación local."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from solventa_common import token_registro

Entorno = Any
SECRETO_LOCAL = token_registro.SECRETO_LOCAL
REGISTRO = "00000000-0000-4000-8000-0000000000aa"
CORRELACION = "corr-m06-vinculo"


def test_vincula_el_sub_del_token_con_su_registro(entorno: Entorno) -> None:
    respuesta = entorno.vincular(entorno.emitir(), cuerpo=None)

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "clienteId": str(entorno.cliente_id),
        "estado": "VERIFICADO",
        "verificadoEn": "2026-10-01T00:00:00Z",
    }
    solicitud = entorno.registro_vinculacion.calls.last.request
    assert solicitud.url.path == f"/v1/registros/{REGISTRO}/vinculacion"
    assert solicitud.headers["X-Actor-Id"] == entorno.sub
    assert solicitud.headers["X-Actor-Rol"] == "CLIENTE"
    assert b"v1.token-sintetico" in solicitud.read()


def test_el_sub_es_el_del_token_nunca_uno_del_cuerpo(entorno: Entorno) -> None:
    cuerpo = {"registroId": REGISTRO, "tokenRegistro": "x", "sub": "ajeno", "clienteId": "ajeno"}

    entorno.vincular(entorno.emitir(), cuerpo=cuerpo)

    solicitud = entorno.registro_vinculacion.calls.last.request
    assert solicitud.headers["X-Actor-Id"] == entorno.sub
    assert b"ajeno" not in solicitud.read()


def test_exige_autenticacion_antes_de_validar_el_cuerpo(entorno: Entorno) -> None:
    respuesta = entorno.vincular(sub=None, cuerpo={})

    assert respuesta.status_code == 401
    assert respuesta.json()["codigo"] == "NO_AUTENTICADO"
    assert entorno.registro_vinculacion.call_count == 0


def test_token_de_otro_pool_responde_403(entorno: Entorno) -> None:
    respuesta = entorno.vincular(entorno.emitir(emisor=entorno.emisor_backoffice))

    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"
    assert entorno.registro_vinculacion.call_count == 0


def test_token_de_registro_invalido_responde_403_con_el_motivo(entorno: Entorno) -> None:
    entorno.registro_vinculacion.mock(
        return_value=httpx.Response(
            403, json={"codigo": "TOKEN_REGISTRO_INVALIDO", "detalles": {"motivo": "VENCIDO"}}
        )
    )

    respuesta = entorno.vincular(entorno.emitir())

    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "TOKEN_REGISTRO_INVALIDO"
    assert respuesta.json()["detalles"] == {"motivo": "VENCIDO"}


def test_registro_ya_vinculado_responde_409(entorno: Entorno) -> None:
    entorno.registro_vinculacion.mock(
        return_value=httpx.Response(409, json={"codigo": "REGISTRO_YA_VINCULADO"})
    )

    respuesta = entorno.vincular(entorno.emitir())

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "REGISTRO_YA_VINCULADO"


@pytest.mark.parametrize(
    "falla",
    [
        {"side_effect": httpx.ReadTimeout("sin respuesta")},
        {"side_effect": httpx.ConnectError("caído")},
        {"return_value": httpx.Response(503)},
        {"return_value": httpx.Response(403, json={"codigo": "ROL_NO_AUTORIZADO"})},
        {"return_value": httpx.Response(200, text="<html>")},
    ],
)
def test_falla_de_clientes_responde_503_y_se_puede_reintentar(
    entorno: Entorno, falla: dict[str, Any]
) -> None:
    entorno.registro_vinculacion.mock(**falla)

    respuesta = entorno.vincular(entorno.emitir())

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert entorno.registro_vinculacion.call_count == 1


@pytest.mark.parametrize(
    "cuerpo",
    [
        {},
        {"registroId": "no-uuid", "tokenRegistro": "x"},
        {"registroId": REGISTRO},
        {"registroId": REGISTRO, "tokenRegistro": ""},
        {"registroId": REGISTRO, "tokenRegistro": "x" * 2049},
    ],
)
def test_cuerpo_invalido_responde_422(entorno: Entorno, cuerpo: dict[str, Any]) -> None:
    respuesta = entorno.vincular(entorno.emitir(), cuerpo=cuerpo)

    assert respuesta.status_code == 422
    assert entorno.registro_vinculacion.call_count == 0


@pytest.mark.parametrize(
    ("preparar", "resultado"),
    [
        (lambda e: None, "ok"),
        (
            lambda e: e.registro_vinculacion.mock(
                return_value=httpx.Response(409, json={"codigo": "REGISTRO_YA_VINCULADO"})
            ),
            "rechazado",
        ),
        (lambda e: e.registro_vinculacion.mock(side_effect=httpx.ReadTimeout("lento")), "timeout"),
        (lambda e: e.registro_vinculacion.mock(return_value=httpx.Response(503)), "error"),
    ],
)
def test_cierra_la_operacion_con_su_resultado(
    entorno: Entorno, preparar: Callable[[Entorno], Any], resultado: str
) -> None:
    preparar(entorno)

    entorno.vincular(entorno.emitir())

    cierre = [
        log for log in entorno.telemetria.logs() if log.get("operation") == "registro.vincular"
    ]
    assert cierre[-1]["result"] == resultado


# --- Simulación local del Lambda pre sign-up (POST /v1/registro/validaciones) ---------------


def _token(correo: str = "persona@ejemplo.co", ttl_s: int = 1800) -> str:
    token, _ = token_registro.emitir(SECRETO_LOCAL, REGISTRO, "cliente-1", correo, ttl_s=ttl_s)
    return token


def test_la_validacion_local_acepta_un_token_vigente_del_mismo_correo(
    crear_entorno: Callable[..., Entorno],
) -> None:
    entorno = crear_entorno(environment="local")

    respuesta = entorno.cliente.post(
        "/v1/registro/validaciones",
        json={"tokenRegistro": _token(), "correo": "PERSONA@ejemplo.co"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["valido"] is True
    assert respuesta.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("token", "correo", "motivo"),
    [
        (lambda: _token(), "otra@ejemplo.co", "CORREO"),
        (lambda: _token(ttl_s=-5), "persona@ejemplo.co", "VENCIDO"),
        (lambda: "basura", "persona@ejemplo.co", "FORMATO"),
        (lambda: _token()[:-3] + "AAA", "persona@ejemplo.co", "FIRMA"),
    ],
)
def test_la_validacion_local_rechaza_como_lo_haria_el_lambda(
    crear_entorno: Callable[..., Entorno], token: Callable[[], str], correo: str, motivo: str
) -> None:
    entorno = crear_entorno(environment="local")

    respuesta = entorno.cliente.post(
        "/v1/registro/validaciones", json={"tokenRegistro": token(), "correo": correo}
    )

    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "TOKEN_REGISTRO_INVALIDO"
    assert respuesta.json()["detalles"] == {"motivo": motivo}


def test_la_validacion_no_existe_fuera_de_local(entorno: Entorno) -> None:
    respuesta = entorno.cliente.post(
        "/v1/registro/validaciones", json={"tokenRegistro": _token(), "correo": "a@b.co"}
    )

    assert respuesta.status_code == 404


def test_la_validacion_local_no_figura_en_el_contrato(
    crear_entorno: Callable[..., Entorno],
) -> None:
    rutas = crear_entorno(environment="local").cliente.get("/openapi.json").json()["paths"]

    assert "/v1/registro/validaciones" not in rutas
    assert {"/v1/registro/verificaciones", "/v1/registro/vinculacion"} <= set(rutas)
