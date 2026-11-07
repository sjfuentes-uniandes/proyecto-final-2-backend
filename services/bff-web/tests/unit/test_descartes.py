"""HU-W24 (bff-web): cola de descarte de los eventos paramétricos con pagos-parametricos simulado.

Solo `analistas-siniestros` (los demás grupos, 403 sin llamar a pagos-parametricos); actor, rol y
correlación hacia pagos-parametricos; los 4xx de la lista blanca pasan tal cual y todo lo demás es
503 SERVICIO_NO_DISPONIBLE sin detalles (falla cerrada).
"""

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import descartes as ruta
from bff_web.adapters.outbound.pagos_parametricos import PagosParametricosCliente
from bff_web.application.descartes import Descartes
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
RUTA = "/v1/siniestros/parametricos/descartes"
MENSAJE_ID = "5f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b"
EVENTO_ID = "4b0c9a35-55a4-4a7e-9f1e-0d6c8c6b2a11"
MENSAJE = {
    "messageId": MENSAJE_ID,
    "eventoId": EVENTO_ID,
    "tipo": "EventoParametricoObservado",
    "recibidoEn": "2026-09-13T14:06:12+00:00",
    "intentos": 5,
    "causa": "FORMATO_INVALIDO",
    "detalle": "esquema",
    "poliza": "500-2026-000411",
}
LISTADO = {
    "mensajes": [MENSAJE],
    "porCausa": [{"causa": "FORMATO_INVALIDO", "cantidad": 1}],
    "total": 1,
    "retencionDias": 14,
    "maxIntentos": 5,
}
RESOLUCION = {
    "messageId": MENSAJE_ID,
    "eventoId": EVENTO_ID,
    "accion": "CORREGIDO",
    "causa": "FORMATO_INVALIDO",
    "actorId": "sub-analista",
    "motivo": None,
    "resueltoEn": "2026-09-13T14:10:00Z",
    "corregido": {"eventId": EVENTO_ID, "payload": {"precipitacionMm": 142.7}},
}


def _usuario(grupos: str, sub: str = "sub-analista") -> dict[str, str]:
    return {
        "X-Authenticated-Groups": grupos,
        "X-Authenticated-Issuer": BACKOFFICE,
        "X-Authenticated-Sub": sub,
        "X-Correlation-Id": "corr-bff-w24",
    }


ANALISTA = _usuario("[analistas-siniestros]")


class Simulado:
    def __init__(self) -> None:
        self.pedidos: list[httpx.Request] = []
        self.responder: Callable[[httpx.Request], httpx.Response] = lambda _: httpx.Response(
            200, json=LISTADO
        )

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        return self.responder(pedido)


@pytest.fixture
def pagos(monkeypatch: pytest.MonkeyPatch) -> Iterator[Simulado]:
    simulado = Simulado()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cliente = PagosParametricosCliente(
        "http://pagos-parametricos:8080", 3000, httpx.MockTransport(simulado)
    )
    app.dependency_overrides[ruta.descartes] = lambda: Descartes(cliente)
    yield simulado
    app.dependency_overrides.clear()


def test_el_analista_lista_la_cola_con_su_actor_y_correlacion(pagos: Simulado) -> None:
    respuesta = TestClient(app).get(RUTA, headers=ANALISTA)

    assert (respuesta.status_code, respuesta.json()) == (200, LISTADO)
    (pedido,) = pagos.pedidos
    assert pedido.url.path == "/v1/parametricos/descartes"
    assert pedido.headers["X-Actor-Id"] == "sub-analista"
    assert pedido.headers["X-Actor-Rol"] == "ANALISTA_SINIESTROS"
    assert pedido.headers["X-Correlation-Id"] == "corr-bff-w24"


@pytest.mark.parametrize("grupos", ["[asesores]", "[operacion]", "[suscriptores]", ""])
def test_otros_grupos_no_llegan_a_pagos_parametricos(pagos: Simulado, grupos: str) -> None:
    respuesta = TestClient(app).get(RUTA, headers=_usuario(grupos))

    assert respuesta.status_code == 403
    assert pagos.pedidos == []


def test_el_detalle_pasa_el_contenido_y_la_clave(pagos: Simulado) -> None:
    detalle = {
        "mensaje": MENSAJE,
        "contenido": {"eventId": EVENTO_ID, "payload": {"precipitacionMm": "142,7"}},
        "texto": "{}",
        "claveIdempotencia": EVENTO_ID,
        "verificacion": None,
        "resolucion": None,
    }
    pagos.responder = lambda _: httpx.Response(200, json=detalle)

    respuesta = TestClient(app).get(f"{RUTA}/{MENSAJE_ID}", headers=ANALISTA)

    assert (respuesta.status_code, respuesta.json()) == (200, detalle)
    assert pagos.pedidos[0].url.path == f"/v1/parametricos/descartes/{MENSAJE_ID}"


def test_corregir_envia_cambios_y_motivo(pagos: Simulado) -> None:
    pagos.responder = lambda _: httpx.Response(200, json=RESOLUCION)

    respuesta = TestClient(app).post(
        f"{RUTA}/{MENSAJE_ID}/correccion",
        headers=ANALISTA,
        json={"cambios": {"precipitacionMm": 142.7}, "motivo": "coma decimal"},
    )

    assert (respuesta.status_code, respuesta.json()["accion"]) == (200, "CORREGIDO")
    assert json.loads(pagos.pedidos[0].content) == {
        "cambios": {"precipitacionMm": 142.7},
        "motivo": "coma decimal",
    }


def test_reprocesar_seleccionados(pagos: Simulado) -> None:
    resultado = {
        "resultados": [
            {
                "messageId": MENSAJE_ID,
                "resolucion": {**RESOLUCION, "accion": "REPROCESADO", "corregido": None},
            }
        ]
    }
    pagos.responder = lambda _: httpx.Response(200, json=resultado)

    respuesta = TestClient(app).post(
        f"{RUTA}/reproceso", headers=ANALISTA, json={"messageIds": [MENSAJE_ID]}
    )

    assert (respuesta.status_code, respuesta.json()) == (200, resultado)
    assert json.loads(pagos.pedidos[0].content) == {"messageIds": [MENSAJE_ID]}


def test_descartar_exige_motivo_sin_llamar(pagos: Simulado) -> None:
    respuesta = TestClient(app).post(f"{RUTA}/{MENSAJE_ID}/descarte", headers=ANALISTA, json={})

    assert respuesta.status_code == 422
    assert pagos.pedidos == []


@pytest.mark.parametrize(
    ("status", "codigo"),
    [
        (404, "DESCARTE_NO_ENCONTRADO"),
        (409, "DESCARTE_YA_RESUELTO"),
        (422, "CORRECCION_INVALIDA"),
    ],
)
def test_los_4xx_prometidos_pasan_tal_cual(pagos: Simulado, status: int, codigo: str) -> None:
    cuerpo = {"codigo": codigo, "mensaje": "m", "correlationId": None, "detalles": {"campos": []}}
    pagos.responder = lambda _: httpx.Response(status, json=cuerpo)

    respuesta = TestClient(app).post(
        f"{RUTA}/{MENSAJE_ID}/correccion", headers=ANALISTA, json={"cambios": {"x": 1}}
    )

    assert (respuesta.status_code, respuesta.json()["codigo"]) == (status, codigo)
    assert respuesta.json()["detalles"] == {"campos": []}


def _cae(_: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("lento")


@pytest.mark.parametrize(
    "responder",
    [
        lambda _: httpx.Response(503, json={"codigo": "COLA_NO_DISPONIBLE", "mensaje": "x"}),
        lambda _: httpx.Response(500, text="interno"),
        lambda _: httpx.Response(403, json={"codigo": "ROL_NO_AUTORIZADO", "mensaje": "x"}),
        lambda _: httpx.Response(200, json={"mensajes": "no es una lista"}),
        lambda _: httpx.Response(200, text="no es json"),
        _cae,
    ],
    ids=["503", "500", "403", "fuera-del-dto", "no-json", "timeout"],
)
def test_lo_demas_es_503_sin_detalles(pagos: Simulado, responder: Any) -> None:
    pagos.responder = responder

    respuesta = TestClient(app).get(RUTA, headers=ANALISTA)

    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "SERVICIO_NO_DISPONIBLE")
    assert respuesta.json()["detalles"] == {}
