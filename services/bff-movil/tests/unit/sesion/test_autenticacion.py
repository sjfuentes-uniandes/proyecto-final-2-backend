"""Revalidación del JWT del pool de clientes (F1) y modo local."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest

Entorno = Any


def _sin_firma(entorno: Entorno) -> str:
    ahora = datetime.now(UTC)
    claims = {
        "sub": entorno.sub,
        "iss": entorno.emisor,
        "token_use": "access",
        "exp": ahora + timedelta(minutes=30),
    }
    return jwt.encode(claims, key=None, algorithm="none", headers={"kid": "kid-prueba"})


def _codigo(respuesta: httpx.Response) -> str:
    return str(respuesta.json()["codigo"])


def test_sin_authorization_responde_401(entorno: Entorno) -> None:
    respuesta = entorno.consultar()

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"
    assert entorno.identidad.call_count == 0


def test_authorization_con_otro_esquema_responde_401(entorno: Entorno) -> None:
    respuesta = entorno.consultar(encabezados={"Authorization": "Basic dXN1YXJpbzpjbGF2ZQ=="})

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"


@pytest.mark.hu("M07", "AC4")
def test_token_vencido_exige_autenticacion_completa(entorno: Entorno) -> None:
    respuesta = entorno.consultar(entorno.emitir(vigencia=timedelta(minutes=-5)))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"
    assert entorno.identidad.call_count == 0


@pytest.mark.hu("M07", "AC4")
def test_token_con_firma_invalida_exige_autenticacion_completa(
    entorno: Entorno, otra_llave_privada: Any
) -> None:
    respuesta = entorno.consultar(entorno.emitir(llave=otra_llave_privada))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"
    assert entorno.identidad.call_count == 0


def test_token_sin_algoritmo_de_firma_responde_401(entorno: Entorno) -> None:
    respuesta = entorno.consultar(_sin_firma(entorno))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"


def test_token_mal_formado_responde_401(entorno: Entorno) -> None:
    respuesta = entorno.consultar("esto-no-es-un-jwt")

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"


def test_token_de_otro_sub_que_el_autenticado_responde_401(entorno: Entorno) -> None:
    respuesta = entorno.consultar(entorno.emitir(), sub="22222222-2222-4222-8222-222222222222")

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"
    assert entorno.identidad.call_count == 0


def test_sin_encabezado_de_sub_responde_401(entorno: Entorno) -> None:
    respuesta = entorno.consultar(entorno.emitir(), sub=None)

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"


@pytest.mark.parametrize("token_use", ["id", "access"])
def test_acepta_id_token_y_access_token(entorno: Entorno, token_use: str) -> None:
    respuesta = entorno.consultar(entorno.emitir(token_use=token_use))

    assert respuesta.status_code == 200


def test_token_use_desconocido_responde_401(entorno: Entorno) -> None:
    respuesta = entorno.consultar(entorno.emitir(token_use="refresh"))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"


def test_token_sin_claims_requeridos_responde_401(entorno: Entorno) -> None:
    ahora = datetime.now(UTC)
    claims = {"sub": entorno.sub, "iss": entorno.emisor, "exp": ahora + timedelta(minutes=5)}
    token = jwt.encode(claims, entorno.llave, algorithm="RS256", headers={"kid": "kid-prueba"})

    respuesta = entorno.consultar(token)

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"


def test_emisor_desconocido_responde_401_sin_consultar_su_jwks(entorno: Entorno) -> None:
    token = entorno.emitir(emisor="https://emisor-desconocido.example/pool")

    respuesta = entorno.consultar(token)

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"
    assert entorno.jwks.call_count == 0


def test_kid_que_el_emisor_no_publica_responde_401(entorno: Entorno) -> None:
    respuesta = entorno.consultar(entorno.emitir(kid="kid-desconocido"))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"


def test_token_del_pool_backoffice_responde_403(entorno: Entorno) -> None:
    respuesta = entorno.consultar(entorno.emitir(emisor=entorno.emisor_backoffice))

    assert respuesta.status_code == 403
    assert _codigo(respuesta) == "ROL_NO_AUTORIZADO"
    assert entorno.identidad.call_count == 0


def test_emisor_del_encabezado_debe_coincidir_con_el_del_token(entorno: Entorno) -> None:
    token = entorno.emitir()

    distinto = entorno.consultar(
        token, encabezados={"X-Authenticated-Issuer": entorno.emisor_backoffice}
    )
    igual = entorno.consultar(token, encabezados={"X-Authenticated-Issuer": entorno.emisor})

    assert distinto.status_code == 401
    assert igual.status_code == 200


def test_las_claves_del_emisor_se_guardan_en_cache(entorno: Entorno) -> None:
    for _ in range(3):
        assert entorno.consultar(entorno.emitir()).status_code == 200

    assert entorno.jwks.call_count == 1


def test_sin_jwks_del_emisor_responde_503(entorno: Entorno) -> None:
    entorno.jwks.mock(return_value=httpx.Response(500))

    respuesta = entorno.consultar(entorno.emitir())

    assert respuesta.status_code == 503
    assert _codigo(respuesta) == "SERVICIO_NO_DISPONIBLE"
    assert entorno.identidad.call_count == 0


def test_modo_local_acepta_el_sub_sin_jwt(crear_entorno: Callable[..., Entorno]) -> None:
    entorno = crear_entorno(environment="local")

    respuesta = entorno.consultar()

    assert respuesta.status_code == 200
    assert respuesta.json()["serviciosAutorizados"] == ["POLIZAS", "SINIESTROS", "PRIVACIDAD"]
    assert entorno.jwks.call_count == 0
    expira = datetime.fromisoformat(respuesta.json()["expiraEn"])
    assert timedelta(minutes=29) < expira - datetime.now(UTC) <= timedelta(minutes=30)


@pytest.mark.parametrize("ambiente", ["pruebas", "int", "produccion"])
def test_el_modo_local_no_existe_fuera_de_local(
    crear_entorno: Callable[..., Entorno], ambiente: str
) -> None:
    entorno = crear_entorno(environment=ambiente)

    respuesta = entorno.consultar()

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "NO_AUTENTICADO"
    assert entorno.identidad.call_count == 0


@pytest.mark.parametrize("sub", [None, "", "con espacios", "x" * 129])
def test_modo_local_exige_un_sub_valido(
    crear_entorno: Callable[..., Entorno], sub: str | None
) -> None:
    entorno = crear_entorno(environment="local")

    respuesta = entorno.consultar(sub=sub)

    assert respuesta.status_code == 401


def test_modo_local_con_token_lo_revalida(crear_entorno: Callable[..., Entorno]) -> None:
    entorno = crear_entorno(environment="local")

    respuesta = entorno.consultar(entorno.emitir(vigencia=timedelta(minutes=-5)))

    assert respuesta.status_code == 401
