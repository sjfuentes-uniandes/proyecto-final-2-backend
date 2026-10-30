"""`consulta` verifica como proveedor el pacto de `bff-movil` (lectura de pólizas, HU-M10).

La app real (rutas, DTO, casos de uso y errores) se sirve con uvicorn en un hilo; la lectura es en
memoria y cada estado del proveedor se siembra desde el manejador de estados de Pact. El pacto no
se edita aquí: lo genera `bff-movil` (`make test-contract SERVICE=bff-movil`).
"""

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import uvicorn
from consulta.application.lectura.casos import ConsultarPoliza, ListarPolizas
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada, Vigencia
from consulta.main import crear_app
from fastapi import FastAPI
from pact import Verifier

PACTO = Path(__file__).parents[4] / "contracts" / "pact" / "bff-movil-consulta.json"
CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
OTRO_CLIENTE = UUID("00000000-0000-4000-8000-0000000000aa")
POLIZA = UUID("00000000-0000-4000-8000-0000000000b1")
AJENA = UUID("00000000-0000-4000-8000-0000000000b2")
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
ESTADOS = {
    "el cliente tiene una póliza",
    "el cliente no tiene pólizas",
    "la póliza pertenece a otro cliente",
    "la póliza no existe",
}


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


class LecturaEnMemoria:
    def __init__(self) -> None:
        self.polizas: list[PolizaProyectada] = []

    async def por_id(self, poliza_id: UUID) -> PolizaProyectada | None:
        return next((p for p in self.polizas if p.poliza_id == poliza_id), None)

    async def por_cliente(self, cliente_id: UUID) -> list[PolizaProyectada]:
        return [p for p in self.polizas if p.cliente_id == cliente_id]


def _poliza(poliza_id: UUID, cliente_id: UUID) -> PolizaProyectada:
    return PolizaProyectada(
        poliza_id=poliza_id,
        cliente_id=cliente_id,
        numero="100-2026-000001",
        estado="ACTIVA",
        producto_id="vida-deudor",
        producto_version="1.0.0",
        producto_nombre="Vida deudor",
        ramo="VIDA",
        vigencia=Vigencia(date(2026, 10, 25), date(2027, 10, 24)),
        prima=Decimal("64638.10"),
        suma_asegurada=Decimal(50000000),
        moneda="COP",
        periodicidad="MENSUAL",
        coberturas=(CoberturaProyectada("MUERTE", "Muerte", True),),
        documento_huella="sha256:" + "a" * 64,
        firma_id="FIRMA-0001",
        emitida_en=AHORA,
        proyectada_en=AHORA,
    )


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
        assert hilo.is_alive(), "consulta no arrancó (ver el log del servidor)"
        assert time.monotonic() < limite, "consulta no arrancó a tiempo"
        time.sleep(0.05)
    try:
        yield url
    finally:
        servidor.should_exit = True
        hilo.join(timeout=15)


def test_consulta_cumple_el_pacto_de_bff_movil() -> None:
    assert PACTO.exists(), "Ejecutar primero services/bff-movil/tests/contract (genera el pacto)"
    lectura = LecturaEnMemoria()
    app = crear_app()
    app.state.listar_polizas = ListarPolizas(lectura, RelojFijo())
    app.state.consultar_poliza = ConsultarPoliza(lectura, RelojFijo())
    vistos: list[str] = []

    def estado(state: str, action: str) -> None:
        """Un estado por interacción; uno desconocido falla la verificación."""
        if action != "setup":
            return
        if state not in ESTADOS:
            raise AssertionError(f"estado del proveedor sin sembrar: {state!r}")
        vistos.append(state)
        lectura.polizas = {
            "el cliente tiene una póliza": [_poliza(POLIZA, CLIENTE)],
            "la póliza pertenece a otro cliente": [_poliza(AJENA, OTRO_CLIENTE)],
        }.get(state, [])

    with _servir(app) as url:
        Verifier("consulta", host="127.0.0.1").add_source(PACTO).add_transport(
            url=url
        ).state_handler(estado, teardown=True).verify()
    assert set(vistos) == ESTADOS
