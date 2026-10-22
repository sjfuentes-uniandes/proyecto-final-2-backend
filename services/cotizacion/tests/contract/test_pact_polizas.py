"""CT-W13-02: `cotizacion` verifica como proveedor el pacto de `polizas` (insumos de suscripción).

La app real (rutas, casos de uso y validación) se sirve con uvicorn en un hilo, con los puertos en
memoria de las pruebas: la cotización se calcula con `CalcularPrima` y cada estado del proveedor
se siembra desde el manejador de estados de Pact. El pacto no se edita aquí: lo genera `polizas`
(`make test-contract SERVICE=polizas`).
"""

import asyncio
import dataclasses
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import uvicorn
from cotizacion.config import Settings
from cotizacion.main import Puertos, crear_app
from fastapi import FastAPI
from pact import Verifier

RAIZ = Path(__file__).parents[4]
PACTO = RAIZ / "contracts" / "pact" / "polizas-cotizacion.json"
SINTETICOS = json.loads(
    (RAIZ / "contracts" / "datos-sinteticos" / "cotizaciones-suscripcion.json").read_text(
        encoding="utf-8"
    )
)
COTIZACION = {c["id"]: UUID(c["insumos"]["cotizacionId"]) for c in SINTETICOS["casos"]}
ESTADOS = {
    "existe la cotización S01 de un cliente verificado con su perfil CALCULADO": "S01",
    "existe la cotización S08 de un cliente verificado, calculada sin perfil": "S08",
}
NO_EXISTE = "la cotización 00000000-0000-4000-a000-0000000000ff no existe"
pytestmark = pytest.mark.anyio


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
        assert hilo.is_alive(), "cotizacion no arrancó (ver el log del servidor)"
        assert time.monotonic() < limite, "cotizacion no arrancó a tiempo"
        time.sleep(0.05)
    try:
        yield url
    finally:
        servidor.should_exit = True
        hilo.join(timeout=15)


async def test_cotizacion_cumple_el_pacto_de_polizas(
    escenario: Any, escenario_cotizacion: Any, perfil_c01: Any, nueva_cotizacion: Any
) -> None:
    assert PACTO.exists(), "Ejecutar primero services/polizas/tests/contract (genera el pacto)"
    calcular = escenario_cotizacion.caso()
    # Las cotizaciones de los estados: C01 con su perfil y C01 sin perfil, con los ids del pacto.
    con_perfil, _, _ = await calcular.ejecutar(nueva_cotizacion(perfil_id=perfil_c01.id))
    sin_perfil, _, _ = await calcular.ejecutar(
        nueva_cotizacion(idempotency_key=UUID("00000000-0000-4000-c000-000000000008"))
    )
    sembrables = {
        "S01": dataclasses.replace(con_perfil, id=COTIZACION["S01"]),
        "S08": dataclasses.replace(sin_perfil, id=COTIZACION["S08"]),
    }
    repo = escenario_cotizacion.repo
    puertos = Puertos(
        identidad=escenario_cotizacion.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        ejecutor=escenario.ejecutor,
        perfiles=escenario_cotizacion.perfiles,
        reloj=escenario_cotizacion.reloj,
        productos=escenario_cotizacion.productos,
        cotizaciones=repo,
    )
    vistos: list[str] = []

    def estado(state: str, action: str) -> None:
        """Un estado por interacción; uno desconocido falla la verificación."""
        if action != "setup":
            return
        vistos.append(state)
        repo.cotizaciones.clear()
        if state == NO_EXISTE:
            return
        if state not in ESTADOS:
            raise AssertionError(f"estado del proveedor sin sembrar: {state!r}")
        cotizacion = sembrables[ESTADOS[state]]
        repo.cotizaciones[cotizacion.id] = cotizacion

    with _servir(crear_app(Settings(), puertos=puertos)) as url:
        Verifier("cotizacion", host="127.0.0.1").add_source(PACTO).add_transport(
            url=url
        ).state_handler(estado, teardown=True).verify()
    assert sorted(vistos) == sorted([*ESTADOS, NO_EXISTE])
