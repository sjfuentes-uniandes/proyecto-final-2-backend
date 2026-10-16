"""POST /v1/registro/verificaciones: verificación pública antes de tener cuenta (HU-M06)."""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

Entorno = Any
CORRELACION = "corr-m06-registro"


@pytest.mark.hu("M06", "AC1")
def test_verificado_devuelve_el_token_que_habilita_el_signup(entorno: Entorno) -> None:
    respuesta = entorno.registrar(encabezados={"X-Correlation-Id": CORRELACION})

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "clienteId": str(entorno.cliente_id),
        "estado": "VERIFICADO",
        "resultado": "APROBADO",
        "motivo": None,
        "verificadoEn": "2026-10-01T00:00:00Z",
        "tokenRegistro": "v1.token-sintetico.firma-sintetica",
        "expiraEn": "2026-10-01T00:30:00Z",
    }
    assert respuesta.headers["X-Correlation-Id"] == CORRELACION
    assert respuesta.headers["Cache-Control"] == "no-store"
    assert entorno.registro_verificacion.call_count == 1


def test_es_publica_y_no_confia_en_encabezados_de_identidad(entorno: Entorno) -> None:
    """Sin token ni `X-Authenticated-Sub`: un cliente podría mandarlo, así que no se usa."""
    respuesta = entorno.registrar(
        encabezados={"X-Authenticated-Sub": "otra-persona", "X-Actor-Id": "otra-persona"}
    )

    assert respuesta.status_code == 200
    solicitud = entorno.registro_verificacion.calls.last.request
    assert "x-actor-id" not in solicitud.headers
    assert "x-actor-rol" not in solicitud.headers
    assert "authorization" not in solicitud.headers


@pytest.mark.hu("M06", "AC1")
def test_reenvia_correo_y_evidencia_a_clientes_con_el_registro_en_la_ruta(
    entorno: Entorno,
) -> None:
    entorno.registrar()

    solicitud = entorno.registro_verificacion.calls.last.request
    assert solicitud.url.path == "/v1/registros/00000000-0000-4000-8000-0000000000aa/verificaciones"
    cuerpo = solicitud.read()
    assert b"persona@ejemplo.co" in cuerpo and b"selfie-sintetica-aprobada" in cuerpo
    assert b"registroId" not in cuerpo


@pytest.mark.hu("M06", "AC2")
def test_captura_invalida_no_entrega_token_ni_rechaza(entorno: Entorno) -> None:
    entorno.registro_responde("CAPTURA_INVALIDA", "PENDIENTE", "ILUMINACION_INSUFICIENTE")

    respuesta = entorno.registrar()

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert (cuerpo["estado"], cuerpo["motivo"]) == ("PENDIENTE", "ILUMINACION_INSUFICIENTE")
    assert cuerpo["tokenRegistro"] is None and cuerpo["expiraEn"] is None


def test_rechazo_del_proveedor_no_entrega_token(entorno: Entorno) -> None:
    entorno.registro_responde("RECHAZADO", "RECHAZADO", "ROSTRO_NO_COINCIDE")

    cuerpo = entorno.registrar().json()

    assert (cuerpo["estado"], cuerpo["tokenRegistro"]) == ("RECHAZADO", None)


@pytest.mark.hu("M06", "AC3")
@pytest.mark.parametrize(
    "falla",
    [
        {"side_effect": httpx.ReadTimeout("sin respuesta")},
        {"side_effect": httpx.ConnectError("caído")},
        {"return_value": httpx.Response(503, json={"codigo": "DEPENDENCIA_NO_DISPONIBLE"})},
        {"return_value": httpx.Response(200, text="<html>")},
        {"return_value": httpx.Response(422, json={"codigo": "SOLICITUD_INVALIDA"})},
    ],
)
def test_falla_recuperable_responde_503_y_permite_reintentar(
    entorno: Entorno, falla: dict[str, Any]
) -> None:
    entorno.registro_verificacion.mock(**falla)

    respuesta = entorno.registrar()

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert entorno.registro_verificacion.call_count == 1  # un timeout nunca se repite

    entorno.registro_responde("APROBADO", "VERIFICADO")
    reintento = entorno.registrar()
    assert reintento.status_code == 200
    assert reintento.json()["clienteId"] == str(entorno.cliente_id)


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        ("REGISTRO_CORREO_DISTINTO", "REGISTRO_CORREO_DISTINTO"),
        ("REGISTRO_YA_VINCULADO", "REGISTRO_YA_VINCULADO"),
    ],
)
def test_conflictos_del_registro(entorno: Entorno, codigo: str, esperado: str) -> None:
    entorno.registro_verificacion.mock(
        return_value=httpx.Response(409, json={"codigo": codigo, "detalles": {}})
    )

    respuesta = entorno.registrar()

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == esperado


def test_un_409_desconocido_es_servicio_no_disponible(entorno: Entorno) -> None:
    entorno.registro_verificacion.mock(return_value=httpx.Response(409, text="no-json"))

    assert entorno.registrar().status_code == 503


def test_el_timeout_de_la_verificacion_no_se_recorta_a_700_ms(entorno: Entorno) -> None:
    entorno.registrar()

    assert entorno.registro_verificacion.calls.last.request.extensions["timeout"]["read"] == 5.0


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

    entorno.registro_verificacion.mock(side_effect=lenta)

    respuesta = entorno.registrar()

    assert respuesta.status_code == 503
    assert llamadas == 1


@pytest.mark.parametrize(
    "cambios",
    [
        {"registroId": "no-uuid"},
        {"correo": "no-es-correo"},
        {"correo": ""},
        {"selfie": ""},
        {"pruebaDeVida": "no-es-objeto"},
    ],
)
def test_cuerpo_invalido_responde_422_sin_llamar_a_clientes(
    entorno: Entorno, cambios: dict[str, Any]
) -> None:
    respuesta = entorno.registrar(cuerpo=entorno.cuerpo_registro(**cambios))

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert entorno.registro_verificacion.call_count == 0


@pytest.mark.hu("M06", "AC4")
def test_evidencia_correo_y_token_no_quedan_en_logs_ni_spans(entorno: Entorno) -> None:
    cuerpo = entorno.cuerpo_registro(
        correo="centinela-734519@ejemplo.co",
        selfie="selfie-centinela-734519",
        pruebaDeVida={"gesto": "centinela-734519"},
    )

    assert entorno.registrar(cuerpo=cuerpo).status_code == 200
    entorno.registro_verificacion.mock(side_effect=httpx.ReadTimeout("sin respuesta"))
    assert entorno.registrar(cuerpo=cuerpo).status_code == 503

    for texto in (entorno.telemetria.texto_logs(), entorno.telemetria.texto_spans()):
        assert "734519" not in texto
        assert "v1.token-sintetico" not in texto


@pytest.mark.parametrize(
    ("preparar", "resultado"),
    [
        (lambda e: None, "ok"),
        (lambda e: e.registro_responde("CAPTURA_INVALIDA", "PENDIENTE", "X"), "rechazado"),
        (
            lambda e: e.registro_verificacion.mock(
                return_value=httpx.Response(409, json={"codigo": "REGISTRO_YA_VINCULADO"})
            ),
            "rechazado",
        ),
        (lambda e: e.registro_verificacion.mock(side_effect=httpx.ReadTimeout("lento")), "timeout"),
        (lambda e: e.registro_verificacion.mock(return_value=httpx.Response(503)), "error"),
    ],
)
def test_cierra_la_operacion_con_su_resultado(
    entorno: Entorno, preparar: Callable[[Entorno], Any], resultado: str
) -> None:
    preparar(entorno)

    entorno.registrar()

    cierre = [
        log for log in entorno.telemetria.logs() if log.get("operation") == "registro.verificar"
    ]
    assert cierre[-1]["result"] == resultado
