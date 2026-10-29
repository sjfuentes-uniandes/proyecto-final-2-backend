"""HU-W16: `adaptador-pagos` verifica como proveedor el pacto de `dinero`: el cobro (16.10) y la
consulta de la conciliación (16.11).

La app real (rutas, caso de uso, validación y traducción de la respuesta del proveedor) se sirve con
uvicorn en un hilo; el proveedor de pagos externo se simula con `httpx.MockTransport`, como en el
Schemathesis de este servicio. Cada estado del proveedor fija qué responde el proveedor externo. El
pacto no se edita aquí: lo genera `dinero` (`make test-contract SERVICE=dinero`).
"""

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn
from adaptador_pagos.config import Settings
from adaptador_pagos.main import crear_app
from fastapi import FastAPI
from pact import Verifier

PACTO = Path(__file__).parents[4] / "contracts" / "pact" / "dinero-adaptador-pagos.json"
# Un cuerpo es un 200 del proveedor; un httpx.Response, cualquier otra respuesta (16.11).
Respuesta = Callable[[str], dict[str, object] | httpx.Response]


def _aprobado(referencia: str) -> dict[str, object]:
    return {
        "referencia": referencia,
        "estado": "APROBADO",
        "transaccionId": "TX-SINT-C01",
        "motivo": None,
    }


def _rechazado(referencia: str) -> dict[str, object]:
    return {
        "referencia": referencia,
        "estado": "RECHAZADO",
        "transaccionId": None,
        "motivo": "FONDOS_INSUFICIENTES",
    }


def _no_encontrado(referencia: str) -> httpx.Response:
    # El proveedor (simulador, consulta de C07): el cobro nunca llegó.
    return httpx.Response(404, json={"error": "cobro_no_encontrado"})


ESTADOS: dict[str, Respuesta] = {
    "el proveedor de pagos aprueba el cobro": _aprobado,
    "el proveedor de pagos rechaza el cobro por fondos insuficientes": _rechazado,
    # 16.11: la consulta (GET /pagos/v1/cobros?referencia=&pagador=).
    "el proveedor de pagos tiene el cobro aprobado": _aprobado,
    "el proveedor de pagos tiene el cobro rechazado por fondos insuficientes": _rechazado,
    "el proveedor de pagos no tiene el cobro": _no_encontrado,
}


@contextmanager
def _servir(app: FastAPI) -> Iterator[str]:
    zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    zocalo.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{zocalo.getsockname()[1]}"
    servidor = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    def correr() -> None:
        asyncio.run(servidor.serve(sockets=[zocalo]), loop_factory=asyncio.SelectorEventLoop)

    hilo = threading.Thread(target=correr, daemon=True)
    hilo.start()
    limite = time.monotonic() + 30
    while not servidor.started:
        assert hilo.is_alive(), "adaptador-pagos no arrancó (ver el log del servidor)"
        assert time.monotonic() < limite, "adaptador-pagos no arrancó a tiempo"
        time.sleep(0.05)
    try:
        yield url
    finally:
        servidor.should_exit = True
        hilo.join(timeout=15)


def test_adaptador_pagos_cumple_el_pacto_de_dinero() -> None:
    assert PACTO.exists(), "Ejecutar primero services/dinero/tests/contract (genera el pacto)"
    actual: dict[str, Respuesta] = {}
    vistos: list[str] = []
    recibidas: list[httpx.Request] = []

    def proveedor(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        if request.method == "GET":
            referencia = request.url.params["referencia"]
        else:
            referencia = json.loads(request.content)["referencia"]
        respuesta = actual["respuesta"](referencia)
        if isinstance(respuesta, httpx.Response):
            return respuesta
        return httpx.Response(200, json=respuesta)

    def estado(state: str, action: str) -> None:
        """Un estado por interacción; uno desconocido falla la verificación."""
        if action != "setup":
            actual.clear()
            return
        if state not in ESTADOS:
            raise AssertionError(f"estado del proveedor sin simular: {state!r}")
        vistos.append(state)
        actual["respuesta"] = ESTADOS[state]

    app = crear_app(Settings(), transport=httpx.MockTransport(proveedor))
    with _servir(app) as url:
        Verifier("adaptador-pagos", host="127.0.0.1").add_source(PACTO).add_transport(
            url=url
        ).state_handler(estado, teardown=True).verify()
    assert sorted(vistos) == sorted(ESTADOS)
    # El adaptador llamó al proveedor una vez por interacción; el cobro, con la clave como
    # idempotencia, y la consulta, sin ella.
    assert len(recibidas) == len(ESTADOS)
    cobros = [r for r in recibidas if r.method == "POST"]
    consultas = [r for r in recibidas if r.method == "GET"]
    assert (len(cobros), len(consultas)) == (2, 3)
    assert all(r.headers["Idempotency-Key"] == json.loads(r.content)["referencia"] for r in cobros)
    assert all("Idempotency-Key" not in r.headers for r in consultas)
