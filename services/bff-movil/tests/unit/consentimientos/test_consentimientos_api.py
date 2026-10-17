"""Rutas /v1/consentimientos: reenvío a `clientes`, traducción de errores, auth y privacidad (HU-M08)."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

Entorno = Any
CORRELACION = "corr-m08-consentimiento"
CONSENTIMIENTO_ID = "00000000-0000-4000-9000-000000000001"
CLAVE = "6f1c2b9e-3d4a-4e8f-9b7c-1a2b3c4d5e6f"
RUTAS = [
    ("POST", "/v1/consentimientos"),
    ("GET", "/v1/consentimientos"),
    ("GET", f"/v1/consentimientos/{CONSENTIMIENTO_ID}"),
    ("GET", "/v1/consentimientos/texto-vigente"),
]


def _error(codigo: str, detalles: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"codigo": codigo, "mensaje": "x", "correlationId": "c", "detalles": detalles or {}}


def _consentimiento(**cambios: Any) -> dict[str, Any]:
    return {
        "consentimientoId": CONSENTIMIENTO_ID,
        "estado": "ACTIVO",
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": ["FA_PRODUCTOS_VIGENTES"],
        "versionTexto": "2026-10-17.1",
        "idioma": "es-CO",
        "otorgadoEn": "2026-10-17T15:00:00Z",
        "vigenteHasta": "2027-10-17T23:59:59Z",
        "revocadoEn": None,
        **cambios,
    }


def _cerrada(entorno: Entorno, operacion: str) -> str:
    cierre = [log for log in entorno.telemetria.logs() if log.get("operation") == operacion]
    return cierre[-1]["result"]


# --- Otorgar --------------------------------------------------------------------------------


@pytest.mark.hu("M08", "AC1")
def test_otorgar_reenvia_al_titular_del_token_y_responde_201(entorno: Entorno) -> None:
    respuesta = entorno.otorgar(encabezados={"X-Correlation-Id": CORRELACION})

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["consentimientoId"] == CONSENTIMIENTO_ID
    assert cuerpo["estado"] == "ACTIVO"
    assert cuerpo["versionTexto"] == "2026-10-17.1"
    assert cuerpo["vigenteHasta"] == "2027-10-17T23:59:59Z"
    assert cuerpo["revocadoEn"] is None
    assert "Idempotent-Replayed" not in respuesta.headers
    solicitud = entorno.otorgamiento.calls.last.request
    assert solicitud.headers["X-Actor-Id"] == entorno.sub
    assert solicitud.headers["X-Actor-Rol"] == "CLIENTE"
    assert solicitud.headers["Idempotency-Key"] == CLAVE
    assert solicitud.headers["X-Correlation-Id"] == CORRELACION
    assert respuesta.headers["X-Correlation-Id"] == CORRELACION


@pytest.mark.hu("M08", "AC1")
def test_otorgar_reenvia_el_cuerpo_sin_transformarlo(entorno: Entorno) -> None:
    entorno.otorgar()

    assert json.loads(entorno.otorgamiento.calls.last.request.read()) == {
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": ["FA_PRODUCTOS_VIGENTES"],
        "vigenciaMeses": 12,
        "versionTexto": "2026-10-17.1",
        "idioma": "es-CO",
    }


@pytest.mark.hu("M08", "AC4")
def test_repetir_la_clave_responde_200_con_idempotent_replayed(entorno: Entorno) -> None:
    entorno.otorgamiento.mock(
        return_value=httpx.Response(
            200, json=_consentimiento(), headers={"Idempotent-Replayed": "true"}
        )
    )

    respuesta = entorno.otorgar()

    assert respuesta.status_code == 200
    assert respuesta.headers["Idempotent-Replayed"] == "true"
    assert respuesta.json()["consentimientoId"] == CONSENTIMIENTO_ID
    assert entorno.otorgamiento.call_count == 1


def test_el_titular_sale_del_token_y_no_del_cuerpo(entorno: Entorno) -> None:
    cuerpo = {
        "proposito": "PERFILAMIENTO_PRECIO",
        "clienteId": "ajeno",
        "sub": "ajeno",
        "actorId": "ajeno",
    }

    entorno.otorgar(cuerpo=cuerpo, encabezados={"X-Actor-Id": "ajeno"})

    solicitud = entorno.otorgamiento.calls.last.request
    assert solicitud.headers["X-Actor-Id"] == entorno.sub
    assert b"ajeno" not in solicitud.read()


@pytest.mark.hu("M08", "AC3")
def test_los_faltantes_los_informa_clientes_con_todos_sus_campos(entorno: Entorno) -> None:
    campos = ["proposito", "alcances", "vigenciaMeses"]
    entorno.otorgamiento.mock(
        return_value=httpx.Response(422, json=_error("SOLICITUD_INVALIDA", {"campos": campos}))
    )

    respuesta = entorno.otorgar(cuerpo={})

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert respuesta.json()["detalles"] == {"campos": campos}
    assert entorno.otorgamiento.call_count == 1


@pytest.mark.parametrize(
    ("clave", "campo"),
    [(None, "Idempotency-Key"), ("no-es-uuid", "Idempotency-Key")],
)
def test_sin_clave_valida_responde_422_sin_llamar_a_clientes(
    entorno: Entorno, clave: str | None, campo: str
) -> None:
    respuesta = entorno.otorgar(clave=clave)

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert campo in respuesta.json()["detalles"]["campos"]
    assert entorno.otorgamiento.call_count == 0


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"vigenciaMeses": "doce"},
        {"vigenciaMeses": False},
        {"alcances": "FA_PRODUCTOS_VIGENTES"},
        {"alcances": ["x"] * 17},
        {"proposito": "x" * 65},
        [],
    ],
)
def test_un_cuerpo_con_tipos_invalidos_responde_422_sin_llamar_a_clientes(
    entorno: Entorno, cuerpo: Any
) -> None:
    respuesta = entorno.otorgar(cuerpo=cuerpo)

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert entorno.otorgamiento.call_count == 0


@pytest.mark.parametrize(
    ("estado", "codigo", "detalles_clientes", "estado_bff", "detalles_bff"),
    [
        (403, "IDENTIDAD_NO_VERIFICADA", {}, 403, {}),
        (404, "CLIENTE_NO_ENCONTRADO", {}, 404, {}),
        (
            409,
            "CONSENTIMIENTO_YA_VIGENTE",
            {"consentimientoId": CONSENTIMIENTO_ID},
            409,
            {"consentimientoId": CONSENTIMIENTO_ID},
        ),
        (422, "VIGENCIA_INVALIDA", {}, 422, {}),
        (422, "TEXTO_NO_VIGENTE", {}, 422, {}),
        (422, "IDEMPOTENCIA_CONFLICTO", {}, 422, {}),
    ],
)
def test_los_rechazos_de_clientes_conservan_su_codigo(
    entorno: Entorno,
    estado: int,
    codigo: str,
    detalles_clientes: dict[str, Any],
    estado_bff: int,
    detalles_bff: dict[str, Any],
) -> None:
    entorno.otorgamiento.mock(
        return_value=httpx.Response(estado, json=_error(codigo, detalles_clientes))
    )

    respuesta = entorno.otorgar(encabezados={"X-Correlation-Id": CORRELACION})

    assert respuesta.status_code == estado_bff
    assert respuesta.json()["codigo"] == codigo
    assert respuesta.json()["detalles"] == detalles_bff
    assert respuesta.json()["correlationId"] == CORRELACION
    assert _cerrada(entorno, "consentimientos.otorgar") == "rechazado"


@pytest.mark.parametrize(
    ("falla", "resultado"),
    [
        ({"side_effect": httpx.ReadTimeout("sin respuesta")}, "timeout"),
        ({"side_effect": httpx.ConnectError("caído")}, "error"),
        ({"return_value": httpx.Response(503, json=_error("DEPENDENCIA_NO_DISPONIBLE"))}, "error"),
        ({"return_value": httpx.Response(500, json=_error("ERROR_INTERNO"))}, "error"),
        ({"return_value": httpx.Response(401, json=_error("ACTOR_REQUERIDO"))}, "error"),
        ({"return_value": httpx.Response(403, json=_error("ROL_NO_AUTORIZADO"))}, "error"),
        ({"return_value": httpx.Response(404, json=_error("NO_ENCONTRADO"))}, "error"),
        ({"return_value": httpx.Response(409, json=_error("CONSENTIMIENTO_YA_VIGENTE"))}, "error"),
        ({"return_value": httpx.Response(422, json=_error("OTRO_CODIGO"))}, "error"),
        ({"return_value": httpx.Response(422, json={"detail": []})}, "error"),
        ({"return_value": httpx.Response(201, text="<html>")}, "error"),
        ({"return_value": httpx.Response(201, json={"consentimientoId": "x"})}, "error"),
    ],
)
def test_una_falla_de_clientes_responde_503_y_falla_cerrado(
    entorno: Entorno, falla: dict[str, Any], resultado: str
) -> None:
    entorno.otorgamiento.mock(**falla)

    respuesta = entorno.otorgar()

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {}
    assert entorno.otorgamiento.call_count == 1
    assert _cerrada(entorno, "consentimientos.otorgar") == resultado


def test_otorgar_tiene_su_propio_presupuesto_y_no_se_recorta_a_700_ms(entorno: Entorno) -> None:
    entorno.otorgar()

    assert entorno.otorgamiento.calls.last.request.extensions["timeout"]["read"] == 3.0


def test_clientes_lento_al_otorgar_responde_503_sin_reintentar(
    crear_entorno: Callable[..., Entorno],
) -> None:
    entorno = crear_entorno(otorgamiento_timeout_ms=50)
    llamadas = 0

    async def lenta(request: httpx.Request) -> httpx.Response:
        nonlocal llamadas
        llamadas += 1
        await asyncio.sleep(0.5)
        return httpx.Response(201)

    entorno.otorgamiento.mock(side_effect=lenta)

    respuesta = entorno.otorgar()

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert llamadas == 1


def test_otorgar_cierra_la_operacion_ok(entorno: Entorno) -> None:
    entorno.otorgar()

    assert _cerrada(entorno, "consentimientos.otorgar") == "ok"


# --- Listar y detalle -------------------------------------------------------------------------


@pytest.mark.hu("M08", "AC1")
def test_listar_devuelve_un_arreglo_simple_del_titular(entorno: Entorno) -> None:
    respuesta = entorno.pedir(
        "GET", "/v1/consentimientos", encabezados={"X-Correlation-Id": CORRELACION}
    )

    assert respuesta.status_code == 200
    assert isinstance(respuesta.json(), list)
    assert respuesta.json()[0]["consentimientoId"] == CONSENTIMIENTO_ID
    solicitud = entorno.consentimientos.calls.last.request
    assert solicitud.headers["X-Actor-Id"] == entorno.sub
    assert solicitud.headers["X-Actor-Rol"] == "CLIENTE"
    assert solicitud.headers["X-Correlation-Id"] == CORRELACION
    assert _cerrada(entorno, "consentimientos.listar") == "ok"


def test_listar_sin_consentimientos_devuelve_lista_vacia(entorno: Entorno) -> None:
    entorno.consentimientos.mock(return_value=httpx.Response(200, json=[]))

    respuesta = entorno.pedir("GET", "/v1/consentimientos")

    assert (respuesta.status_code, respuesta.json()) == (200, [])


def test_listar_conserva_el_estado_derivado_por_clientes(entorno: Entorno) -> None:
    vencido = _consentimiento(estado="VENCIDO")
    revocado = _consentimiento(estado="REVOCADO", revocadoEn="2026-11-01T10:00:00Z")
    entorno.consentimientos.mock(return_value=httpx.Response(200, json=[vencido, revocado]))

    cuerpo = entorno.pedir("GET", "/v1/consentimientos").json()

    assert [c["estado"] for c in cuerpo] == ["VENCIDO", "REVOCADO"]
    assert cuerpo[1]["revocadoEn"] == "2026-11-01T10:00:00Z"


@pytest.mark.parametrize(
    ("falla", "resultado"),
    [
        ({"side_effect": httpx.ReadTimeout("sin respuesta")}, "timeout"),
        ({"return_value": httpx.Response(503, json=_error("DEPENDENCIA_NO_DISPONIBLE"))}, "error"),
        ({"return_value": httpx.Response(200, json={"items": []})}, "error"),
        ({"return_value": httpx.Response(200, json=[{"estado": "ACTIVO"}])}, "error"),
    ],
)
def test_listar_con_clientes_fallando_responde_503(
    entorno: Entorno, falla: dict[str, Any], resultado: str
) -> None:
    entorno.consentimientos.mock(**falla)

    respuesta = entorno.pedir("GET", "/v1/consentimientos")

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert _cerrada(entorno, "consentimientos.listar") == resultado


def test_las_consultas_usan_el_presupuesto_de_clientes(entorno: Entorno) -> None:
    entorno.pedir("GET", "/v1/consentimientos")

    assert entorno.consentimientos.calls.last.request.extensions["timeout"]["read"] == 0.5


@pytest.mark.hu("M08", "AC1")
def test_detalle_devuelve_el_comprobante(entorno: Entorno) -> None:
    respuesta = entorno.pedir("GET", f"/v1/consentimientos/{CONSENTIMIENTO_ID}")

    assert respuesta.status_code == 200
    assert respuesta.json()["consentimientoId"] == CONSENTIMIENTO_ID
    assert entorno.consentimiento.calls.last.request.headers["X-Actor-Id"] == entorno.sub
    assert _cerrada(entorno, "consentimientos.detalle") == "ok"


def test_detalle_ajeno_o_inexistente_responde_404(entorno: Entorno) -> None:
    entorno.consentimiento.mock(
        return_value=httpx.Response(404, json=_error("CONSENTIMIENTO_NO_ENCONTRADO"))
    )

    respuesta = entorno.pedir("GET", f"/v1/consentimientos/{CONSENTIMIENTO_ID}")

    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "CONSENTIMIENTO_NO_ENCONTRADO"
    assert _cerrada(entorno, "consentimientos.detalle") == "rechazado"


@pytest.mark.parametrize(
    "falla",
    [
        {"side_effect": httpx.ConnectError("caído")},
        {"return_value": httpx.Response(404, json=_error("NO_ENCONTRADO"))},
        {"return_value": httpx.Response(404, text="no json")},
        {"return_value": httpx.Response(500)},
    ],
)
def test_detalle_con_clientes_fallando_responde_503(
    entorno: Entorno, falla: dict[str, Any]
) -> None:
    entorno.consentimiento.mock(**falla)

    respuesta = entorno.pedir("GET", f"/v1/consentimientos/{CONSENTIMIENTO_ID}")

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"


def test_detalle_de_otro_consentimiento_viola_el_contrato_y_falla_cerrado(
    entorno: Entorno,
) -> None:
    otro = _consentimiento(consentimientoId="00000000-0000-4000-9000-000000000002")
    entorno.consentimiento.mock(return_value=httpx.Response(200, json=otro))

    respuesta = entorno.pedir("GET", f"/v1/consentimientos/{CONSENTIMIENTO_ID}")

    assert respuesta.status_code == 503


def test_detalle_con_un_identificador_invalido_responde_422(entorno: Entorno) -> None:
    respuesta = entorno.pedir("GET", "/v1/consentimientos/no-es-uuid")

    assert respuesta.status_code == 422
    assert respuesta.json()["detalles"]["campos"] == ["consentimientoId"]
    assert entorno.consentimiento.call_count == 0


# --- Texto vigente ----------------------------------------------------------------------------


@pytest.mark.hu("M08", "AC1")
def test_texto_vigente_reenvia_proposito_e_idioma(entorno: Entorno) -> None:
    respuesta = entorno.pedir(
        "GET",
        "/v1/consentimientos/texto-vigente",
        params={"proposito": "PERFILAMIENTO_PRECIO", "idioma": "en-US"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["versionTexto"] == "2026-10-17.1"
    assert respuesta.json()["vigenciasMeses"] == [6, 12]
    solicitud = entorno.texto_vigente.calls.last.request
    assert dict(solicitud.url.params) == {"proposito": "PERFILAMIENTO_PRECIO", "idioma": "en-US"}
    # El texto no depende del titular: no se envía el actor (minimización).
    assert "X-Actor-Id" not in solicitud.headers
    assert _cerrada(entorno, "consentimientos.texto_vigente") == "ok"


def test_texto_vigente_sin_parametros_no_los_envia(entorno: Entorno) -> None:
    entorno.pedir("GET", "/v1/consentimientos/texto-vigente")

    assert dict(entorno.texto_vigente.calls.last.request.url.params) == {}


def test_texto_vigente_no_soportado_responde_422_con_sus_campos(entorno: Entorno) -> None:
    entorno.texto_vigente.mock(
        return_value=httpx.Response(422, json=_error("SOLICITUD_INVALIDA", {"campos": ["idioma"]}))
    )

    respuesta = entorno.pedir(
        "GET", "/v1/consentimientos/texto-vigente", params={"idioma": "fr-FR"}
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["detalles"] == {"campos": ["idioma"]}
    assert _cerrada(entorno, "consentimientos.texto_vigente") == "rechazado"


@pytest.mark.parametrize(
    "falla",
    [
        {"side_effect": httpx.ReadTimeout("sin respuesta")},
        {"return_value": httpx.Response(422, json=_error("VIGENCIA_INVALIDA_X"))},
        {"return_value": httpx.Response(200, json={"proposito": "x"})},
        {"return_value": httpx.Response(503)},
    ],
)
def test_texto_vigente_con_clientes_fallando_responde_503(
    entorno: Entorno, falla: dict[str, Any]
) -> None:
    entorno.texto_vigente.mock(**falla)

    respuesta = entorno.pedir("GET", "/v1/consentimientos/texto-vigente")

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"


def test_texto_vigente_no_se_confunde_con_un_identificador(entorno: Entorno) -> None:
    entorno.pedir("GET", "/v1/consentimientos/texto-vigente")

    assert entorno.texto_vigente.call_count == 1
    assert entorno.consentimiento.call_count == 0


# --- Autenticación y privacidad ---------------------------------------------------------------


@pytest.mark.parametrize(("metodo", "ruta"), RUTAS)
def test_sin_token_responde_401_sin_llamar_a_clientes(
    entorno: Entorno, metodo: str, ruta: str
) -> None:
    respuesta = entorno.cliente.request(
        metodo,
        ruta,
        json={},
        headers={"X-Authenticated-Sub": entorno.sub, "Idempotency-Key": CLAVE},
    )

    assert respuesta.status_code == 401
    assert respuesta.json()["codigo"] == "NO_AUTENTICADO"
    for ruta_clientes in (
        entorno.otorgamiento,
        entorno.consentimientos,
        entorno.consentimiento,
        entorno.texto_vigente,
    ):
        assert ruta_clientes.call_count == 0


@pytest.mark.parametrize(("metodo", "ruta"), RUTAS)
def test_token_de_otro_pool_responde_403_sin_llamar_a_clientes(
    entorno: Entorno, metodo: str, ruta: str
) -> None:
    token = entorno.emitir(emisor=entorno.emisor_backoffice)

    respuesta = entorno.pedir(
        metodo, ruta, token, cuerpo={}, encabezados={"Idempotency-Key": CLAVE}
    )

    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"
    assert entorno.otorgamiento.call_count == 0
    assert entorno.consentimientos.call_count == 0


@pytest.mark.parametrize(("metodo", "ruta"), RUTAS)
def test_un_sub_distinto_al_del_token_responde_401(
    entorno: Entorno, metodo: str, ruta: str
) -> None:
    respuesta = entorno.pedir(
        metodo, ruta, sub="otro-sub", cuerpo={}, encabezados={"Idempotency-Key": CLAVE}
    )

    assert respuesta.status_code == 401


def test_el_cuerpo_y_la_clave_no_quedan_en_logs_ni_spans(entorno: Entorno) -> None:
    cuerpo = {"proposito": "PERFILAMIENTO_PRECIO", "versionTexto": "centinela-918273"}
    clave = "918273aa-0000-4000-8000-000000000000"

    assert entorno.otorgar(cuerpo=cuerpo, clave=clave).status_code == 201
    entorno.otorgamiento.mock(side_effect=httpx.ReadTimeout("sin respuesta"))
    assert entorno.otorgar(cuerpo=cuerpo, clave=clave).status_code == 503

    assert "918273" not in entorno.telemetria.texto_logs()
    assert "918273" not in entorno.telemetria.texto_spans()
