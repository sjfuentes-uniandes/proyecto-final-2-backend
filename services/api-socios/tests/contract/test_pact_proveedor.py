import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from api_socios.domain.socios import CanalVenta, Permiso, Plan, Socio
from api_socios.main import app
from pact import Verifier

PACTO = Path(__file__).parents[4] / "contracts" / "pact" / "bff-web-api-socios.json"


@pytest.fixture
def proveedor(entorno) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto = s.getsockname()[1]
    servidor = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=puerto, lifespan="off", log_level="warning")
    )
    hilo = threading.Thread(target=servidor.run, daemon=True)
    hilo.start()
    while not servidor.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{puerto}"
    servidor.should_exit = True
    hilo.join(5)


def test_api_socios_cumple_el_pacto_de_bff_web(entorno, proveedor: str) -> None:
    assert PACTO.exists(), "Ejecutar primero services/bff-web/tests/contract (genera el pacto)"

    def estado(state: str, action: str) -> None:
        entorno.repositorio.socios.clear()
        if action == "setup" and state == "existe el socio SOC-000001":
            entorno.repositorio.guardar(
                Socio(
                    id="SOC-000001",
                    nombre="Fintech Caribe S.A.S.",
                    canal=CanalVenta.PLATAFORMA_DIGITAL,
                    plan=Plan.BASICO,
                    permisos=frozenset({Permiso.COTIZAR}),
                    client_id="cliente-pacto",
                    api_key_id="key-pacto",
                )
            )

    Verifier("api-socios", host="127.0.0.1").add_source(PACTO).add_transport(
        url=proveedor
    ).state_handler(estado, teardown=True).verify()
