"""`avisos` verifica como proveedor el pacto de `bff-movil` (dispositivos, HU-M11).

La app real (rutas, DTO y errores) se sirve con uvicorn en un hilo; los casos de uso se sustituyen
por falsos que cada estado del proveedor configura desde el manejador de estados de Pact. El pacto
no se edita aquí: lo genera `bff-movil` (`make test-contract SERVICE=bff-movil`).
"""

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import uvicorn
from avisos.application.dispositivos.puertos import DispositivoNoAutorizado, DispositivoNoEncontrado
from avisos.main import crear_app
from fastapi import FastAPI
from pact import Verifier

PACTO = Path(__file__).parents[4] / "contracts" / "pact" / "bff-movil-avisos.json"
DISPOSITIVO = UUID("00000000-0000-4000-8000-0000000000d1")
ESTADOS = {
    "el cliente puede registrar dispositivos",
    "el dispositivo es del cliente",
    "el dispositivo pertenece a otro cliente",
    "el dispositivo no existe",
}


class RegistrarFalso:
    async def ejecutar(self, cliente_id: UUID, token: str) -> UUID:
        return DISPOSITIVO


class DarDeBajaFalso:
    def __init__(self) -> None:
        self.error: Exception | None = None

    async def ejecutar(self, cliente_id: UUID, dispositivo_id: UUID) -> None:
        if self.error is not None:
            raise self.error


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
        assert hilo.is_alive(), "avisos no arrancó (ver el log del servidor)"
        assert time.monotonic() < limite, "avisos no arrancó a tiempo"
        time.sleep(0.05)
    try:
        yield url
    finally:
        servidor.should_exit = True
        hilo.join(timeout=15)


def test_avisos_cumple_el_pacto_de_bff_movil() -> None:
    assert PACTO.exists(), "Ejecutar primero services/bff-movil/tests/contract (genera el pacto)"
    baja = DarDeBajaFalso()
    app = crear_app()
    app.state.registrar_dispositivo = RegistrarFalso()
    app.state.dar_de_baja_dispositivo = baja
    vistos: list[str] = []

    def estado(state: str, action: str) -> None:
        """Un estado por interacción; uno desconocido falla la verificación."""
        if action != "setup":
            return
        if state not in ESTADOS:
            raise AssertionError(f"estado del proveedor sin sembrar: {state!r}")
        vistos.append(state)
        baja.error = {
            "el dispositivo pertenece a otro cliente": DispositivoNoAutorizado(),
            "el dispositivo no existe": DispositivoNoEncontrado(),
        }.get(state)

    with _servir(app) as url:
        Verifier("avisos", host="127.0.0.1").add_source(PACTO).add_transport(url=url).state_handler(
            estado, teardown=True
        ).verify()
    assert set(vistos) == ESTADOS
