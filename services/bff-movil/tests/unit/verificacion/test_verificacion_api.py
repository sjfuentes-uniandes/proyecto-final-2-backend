"""POST /v1/identidad/verificaciones: resultado, fallas recuperables, auth y privacidad (HU-M06)."""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

Entorno = Any
CORRELACION = "corr-m06-verificacion"


@pytest.mark.hu("M06", "AC1")
def test_aprobada_devuelve_identidad_verificada_asociada_al_cliente(entorno: Entorno) -> None:
    respuesta = entorno.verificar(entorno.emitir(), encabezados={"X-Correlation-Id": CORRELACION})

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "clienteId": str(entorno.cliente_id),
        "estado": "VERIFICADO",
        "resultado": "APROBADO",
        "motivo": None,
        "verificadoEn": "2026-10-01T00:00:00Z",
    }
    solicitud = entorno.verificacion.calls.last.request
    assert solicitud.headers["X-Actor-Id"] == entorno.sub
    assert solicitud.headers["X-Actor-Rol"] == "CLIENTE"
    assert solicitud.headers["X-Correlation-Id"] == CORRELACION
    assert respuesta.headers["X-Correlation-Id"] == CORRELACION


@pytest.mark.hu("M06", "AC1")
def test_reenvia_la_evidencia_a_clientes_sin_transformarla(entorno: Entorno) -> None:
    entorno.verificar(entorno.emitir())

    cuerpo = entorno.verificacion.calls.last.request.read()
    assert b"selfie-sintetica-aprobada" in cuerpo
    assert b"completado" in cuerpo


@pytest.mark.hu("M06", "AC2")
def test_captura_invalida_no_marca_la_identidad_como_rechazada(entorno: Entorno) -> None:
    entorno.verificacion_responde("CAPTURA_INVALIDA", "PENDIENTE", "ILUMINACION_INSUFICIENTE")

    respuesta = entorno.verificar(entorno.emitir())

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "PENDIENTE"
    assert cuerpo["resultado"] == "CAPTURA_INVALIDA"
    assert cuerpo["motivo"] == "ILUMINACION_INSUFICIENTE"
    assert cuerpo["verificadoEn"] is None


def test_rechazo_del_proveedor(entorno: Entorno) -> None:
    entorno.verificacion_responde("RECHAZADO", "RECHAZADO", "ROSTRO_NO_COINCIDE")

    cuerpo = entorno.verificar(entorno.emitir()).json()

    assert (cuerpo["estado"], cuerpo["resultado"]) == ("RECHAZADO", "RECHAZADO")


@pytest.mark.hu("M06", "AC3")
@pytest.mark.parametrize(
    "falla",
    [
        {"side_effect": httpx.ReadTimeout("sin respuesta")},
        {"side_effect": httpx.ConnectError("caído")},
        {"return_value": httpx.Response(503, json={"codigo": "DEPENDENCIA_NO_DISPONIBLE"})},
        {"return_value": httpx.Response(200, text="<html>")},
    ],
)
def test_falla_recuperable_responde_503_y_permite_reintentar(
    entorno: Entorno, falla: dict[str, Any]
) -> None:
    entorno.verificacion.mock(**falla)

    respuesta = entorno.verificar(entorno.emitir())

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert entorno.verificacion.call_count == 1  # un timeout nunca se repite

    entorno.verificacion_responde("APROBADO", "VERIFICADO")
    reintento = entorno.verificar(entorno.emitir())
    assert reintento.status_code == 200
    assert reintento.json()["clienteId"] == str(entorno.cliente_id)


def test_el_timeout_de_la_verificacion_no_se_recorta_a_700_ms(entorno: Entorno) -> None:
    """`clientes` incluye al proveedor KYC (3 s) y espera 4 s: el BFF espera 5 s."""
    entorno.verificar(entorno.emitir())

    assert entorno.verificacion.calls.last.request.extensions["timeout"]["read"] == 5.0


def test_clientes_que_excede_el_timeout_responde_503_sin_reintentar(
    crear_entorno: Callable[..., Entorno],
) -> None:
    entorno = crear_entorno(verificacion_timeout_ms=50)
    llamadas = 0

    async def lenta(request: httpx.Request) -> httpx.Response:
        nonlocal llamadas
        llamadas += 1
        await asyncio.sleep(0.5)
        return httpx.Response(200)

    entorno.verificacion.mock(side_effect=lenta)

    respuesta = entorno.verificar(entorno.emitir())

    assert respuesta.status_code == 503
    assert llamadas == 1


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"pruebaDeVida": {}},
        {"selfie": "", "pruebaDeVida": {}},
        {"selfie": "x"},
        {"selfie": "x", "pruebaDeVida": "no-es-objeto"},
    ],
)
def test_cuerpo_invalido_responde_422_sin_llamar_a_clientes(
    entorno: Entorno, cuerpo: dict[str, Any]
) -> None:
    respuesta = entorno.verificar(entorno.emitir(), cuerpo=cuerpo)

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert entorno.verificacion.call_count == 0


def test_sin_token_responde_401_antes_de_validar_el_cuerpo(entorno: Entorno) -> None:
    respuesta = entorno.verificar(sub=None, cuerpo={})

    assert respuesta.status_code == 401
    assert respuesta.json()["codigo"] == "NO_AUTENTICADO"
    assert entorno.verificacion.call_count == 0


def test_token_de_otro_pool_responde_403(entorno: Entorno) -> None:
    token = entorno.emitir(emisor=entorno.emisor_backoffice)

    respuesta = entorno.verificar(token)

    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"
    assert entorno.verificacion.call_count == 0


def test_el_sub_del_token_es_el_actor_que_se_envia_a_clientes(entorno: Entorno) -> None:
    """El resultado se asocia al titular del token, nunca a un dato que venga en el cuerpo."""
    cuerpo = {"selfie": "x", "pruebaDeVida": {}, "clienteId": "ajeno", "sub": "ajeno"}

    entorno.verificar(entorno.emitir(), cuerpo=cuerpo)

    assert entorno.verificacion.calls.last.request.headers["X-Actor-Id"] == entorno.sub
    assert b"ajeno" not in entorno.verificacion.calls.last.request.read()


@pytest.mark.hu("M06", "AC4")
def test_la_evidencia_no_queda_en_logs_ni_spans(entorno: Entorno) -> None:
    cuerpo = {"selfie": "selfie-centinela-734519", "pruebaDeVida": {"gesto": "centinela-734519"}}

    assert entorno.verificar(entorno.emitir(), cuerpo=cuerpo).status_code == 200
    entorno.verificacion.mock(side_effect=httpx.ReadTimeout("sin respuesta"))
    assert entorno.verificar(entorno.emitir(), cuerpo=cuerpo).status_code == 503

    assert "734519" not in entorno.telemetria.texto_logs()
    assert "734519" not in entorno.telemetria.texto_spans()


@pytest.mark.parametrize(
    ("preparar", "resultado"),
    [
        (lambda e: None, "ok"),
        (lambda e: e.verificacion_responde("CAPTURA_INVALIDA", "PENDIENTE", "X"), "rechazado"),
        (lambda e: e.verificacion.mock(side_effect=httpx.ReadTimeout("lento")), "timeout"),
        (lambda e: e.verificacion.mock(return_value=httpx.Response(503)), "error"),
    ],
)
def test_cierra_la_operacion_con_su_resultado(
    entorno: Entorno, preparar: Callable[[Entorno], Any], resultado: str
) -> None:
    preparar(entorno)

    entorno.verificar(entorno.emitir())

    cierre = [
        log for log in entorno.telemetria.logs() if log.get("operation") == "verificacion.enviar"
    ]
    assert cierre[-1]["result"] == resultado
