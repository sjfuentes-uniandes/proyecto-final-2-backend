"""HU-W17 (17.8): `adaptador-firma` verifica como proveedor el pacto de `polizas`: la firma de la
huella del documento de la póliza.

La app real (ruta, caso de uso, validación y traducción de la respuesta del proveedor) se sirve con
uvicorn en un hilo; el proveedor de firma externo se simula con `httpx.MockTransport`, como en el
Schemathesis de este servicio. Cada estado del proveedor fija qué responde el proveedor externo. El
pacto no se edita aquí: lo genera `polizas` (`make test-contract SERVICE=polizas`).
"""

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from adaptador_firma.config import Settings
from adaptador_firma.main import crear_app
from fastapi import FastAPI
from pact import Verifier

PACTO = Path(__file__).parents[4] / "contracts" / "pact" / "polizas-adaptador-firma.json"
Respuesta = Callable[[dict[str, Any]], dict[str, object]]


def _firmado(cuerpo: dict[str, Any]) -> dict[str, object]:
    # Como el simulador (C01): devuelve el documento y la huella recibidos.
    return {
        "documentoId": cuerpo["documentoId"],
        "estado": "FIRMADO",
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": "2026-01-01T00:00:00Z",
        "huella": cuerpo["huella"],
    }


ESTADOS: dict[str, Respuesta] = {
    "el proveedor de firma firma la huella del documento": _firmado,
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
        assert hilo.is_alive(), "adaptador-firma no arrancó (ver el log del servidor)"
        assert time.monotonic() < limite, "adaptador-firma no arrancó a tiempo"
        time.sleep(0.05)
    try:
        yield url
    finally:
        servidor.should_exit = True
        hilo.join(timeout=15)


def test_adaptador_firma_cumple_el_pacto_de_polizas() -> None:
    assert PACTO.exists(), "Ejecutar primero services/polizas/tests/contract (genera el pacto)"
    actual: dict[str, Respuesta] = {}
    vistos: list[str] = []
    recibidas: list[httpx.Request] = []

    def proveedor(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json=actual["respuesta"](json.loads(request.content)))

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
        Verifier("adaptador-firma", host="127.0.0.1").add_source(PACTO).add_transport(
            url=url
        ).state_handler(estado, teardown=True).verify()
    assert sorted(vistos) == sorted(ESTADOS)
    # El adaptador llamó al proveedor una vez, con la póliza como clave idempotente y el cliente
    # como firmante.
    [pedido] = recibidas
    cuerpo = json.loads(pedido.content)
    assert pedido.headers["Idempotency-Key"] == cuerpo["documentoId"]
    assert cuerpo["firmante"] == "00000000-0000-4000-8000-000000000001"
