"""HU-W04: `cotizacion` verifica como proveedor el pacto de `api-socios` (la oferta del socio).

La app real (rutas, casos de uso y validación) se sirve con uvicorn en un hilo, con los puertos en
memoria de las pruebas. Cada estado se siembra desde el manejador de estados de Pact: las reglas
2026.10.1 (con condiciones de oferta) vigentes y las cotizaciones del socio o de otro socio,
calculadas con `CalcularPrima`. El pacto no se edita aquí: lo genera `api-socios`
(`make test-contract SERVICE=api-socios`).
"""

import asyncio
import dataclasses
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
from cotizacion.domain.cotizacion.modelos import Actor, RolActor
from cotizacion.main import Puertos, crear_app
from fastapi import FastAPI
from pact import Verifier

PACTO = Path(__file__).parents[4] / "contracts" / "pact" / "api-socios-cotizacion.json"
DEL_SOCIO = UUID("00000000-0000-4000-d000-000000000004")
DE_OTRO = UUID("00000000-0000-4000-d000-000000000005")
REGLAS_CON_OFERTA = "las reglas vigentes de vida-hipotecario traen condiciones de oferta"
ESTADO_DEL_SOCIO = f"existe la cotización {DEL_SOCIO} del socio SOC-000001 con su oferta"
ESTADO_DE_OTRO = f"existe la cotización {DE_OTRO} de otro socio"
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


def _con_id(cotizacion: Any, cotizacion_id: UUID) -> Any:
    oferta = dataclasses.replace(cotizacion.oferta, cotizacion_id=cotizacion_id)
    return dataclasses.replace(cotizacion, id=cotizacion_id, oferta=oferta)


async def test_cotizacion_cumple_el_pacto_de_api_socios(
    escenario: Any, escenario_cotizacion: Any, nueva_cotizacion: Any, semilla_w03: Any
) -> None:
    assert PACTO.exists(), "Ejecutar primero services/api-socios/tests/contract (genera el pacto)"
    _, reglas_semilla = semilla_w03
    productos, repo = escenario_cotizacion.productos, escenario_cotizacion.repo
    productos.reglas["vida-hipotecario"] = reglas_semilla(
        indice=-1, vigente_desde=escenario_cotizacion.reloj.ahora()
    )
    calcular = escenario_cotizacion.caso()

    async def del_socio(socio: str) -> Any:
        cotizacion, _, _ = await calcular.ejecutar(
            nueva_cotizacion(
                actor=Actor(socio, RolActor.SOCIO),
                coberturas_opcionales=("ENFERMEDADES_GRAVES",),
            )
        )
        return cotizacion

    sembrables = {
        ESTADO_DEL_SOCIO: _con_id(await del_socio("SOC-000001"), DEL_SOCIO),
        ESTADO_DE_OTRO: _con_id(await del_socio("SOC-000002"), DE_OTRO),
    }
    puertos = Puertos(
        identidad=escenario_cotizacion.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        ejecutor=escenario.ejecutor,
        perfiles=escenario_cotizacion.perfiles,
        reloj=escenario_cotizacion.reloj,
        productos=productos,
        cotizaciones=repo,
    )
    vistos: list[str] = []

    def estado(state: str, action: str) -> None:
        """Un estado por interacción; uno desconocido falla la verificación."""
        if action != "setup":
            return
        vistos.append(state)
        repo.cotizaciones.clear()
        if state == REGLAS_CON_OFERTA:
            return
        if state not in sembrables:
            raise AssertionError(f"estado del proveedor sin sembrar: {state!r}")
        cotizacion = sembrables[state]
        repo.cotizaciones[cotizacion.id] = cotizacion

    with _servir(crear_app(Settings(), puertos=puertos)) as url:
        Verifier("cotizacion", host="127.0.0.1").add_source(PACTO).add_transport(
            url=url
        ).state_handler(estado, teardown=True).verify()
    assert sorted(set(vistos)) == sorted([REGLAS_CON_OFERTA, *sembrables])
