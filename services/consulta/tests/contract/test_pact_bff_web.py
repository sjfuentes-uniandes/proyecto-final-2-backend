"""`consulta` verifica como proveedor el pacto de `bff-web` (estado y listado de pólizas, HU-W18).

La app real (rutas, DTO, casos de uso y errores) se sirve con uvicorn en un hilo; la lectura es en
memoria, aplica el filtro como el adaptador de Postgres y cada estado del proveedor se siembra
desde el manejador de estados de Pact. El pacto no se edita aquí: lo genera `bff-web`
(`make test-contract SERVICE=bff-web`).
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
from consulta.application.estado.casos import BuscarPolizas, ConsultarEstadoPoliza
from consulta.application.estado.puertos import FiltroPolizas, ResultadoBusqueda
from consulta.domain.polizas.estado import estado_de
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada, Vigencia
from consulta.main import crear_app
from fastapi import FastAPI
from pact import Verifier

PACTO = Path(__file__).parents[4] / "contracts" / "pact" / "bff-web-consulta.json"
POLIZA = UUID("00000000-0000-4000-8000-0000000000b1")
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
ESTADOS = {
    "existe una póliza proyectada",
    "la póliza no existe",
    "hay pólizas proyectadas",
    "no hay pólizas proyectadas",
}


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


class LecturaEnMemoria:
    def __init__(self) -> None:
        self.polizas: list[PolizaProyectada] = []

    async def por_id(self, poliza_id: UUID) -> PolizaProyectada | None:
        return next((p for p in self.polizas if p.poliza_id == poliza_id), None)

    async def buscar(self, filtro: FiltroPolizas, pagina: int, tamano: int) -> ResultadoBusqueda:
        coincidentes = [
            p
            for p in self.polizas
            if (filtro.numero is None or p.numero == filtro.numero)
            and (filtro.ramo is None or p.ramo == filtro.ramo)
            and (filtro.estado is None or estado_de(p.vigencia, filtro.hoy, 30) is filtro.estado)
            and (filtro.vigencia_desde is None or p.vigencia.hasta >= filtro.vigencia_desde)
        ]
        coincidentes.sort(key=lambda p: (-p.emitida_en.timestamp(), str(p.poliza_id)))
        inicio = (pagina - 1) * tamano
        actualizado = max((p.proyectada_en for p in self.polizas), default=None)
        return ResultadoBusqueda(
            tuple(coincidentes[inicio : inicio + tamano]), len(coincidentes), actualizado
        )


def _poliza() -> PolizaProyectada:
    return PolizaProyectada(
        poliza_id=POLIZA,
        cliente_id=UUID("00000000-0000-4000-8000-000000000001"),
        numero="100-2026-000001",
        estado="ACTIVA",
        producto_id="vida-deudor",
        producto_version="1.0.0",
        producto_nombre="Vida deudor",
        ramo="Vida",
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


def test_consulta_cumple_el_pacto_de_bff_web() -> None:
    assert PACTO.exists(), "Ejecutar primero services/bff-web/tests/contract (genera el pacto)"
    lectura = LecturaEnMemoria()
    app = crear_app()
    app.state.consultar_estado_poliza = ConsultarEstadoPoliza(lectura, RelojFijo(), 30)
    app.state.buscar_polizas = BuscarPolizas(lectura, RelojFijo(), 30)
    vistos: list[str] = []

    def estado(state: str, action: str) -> None:
        """Un estado por interacción; uno desconocido falla la verificación."""
        if action != "setup":
            return
        if state not in ESTADOS:
            raise AssertionError(f"estado del proveedor sin sembrar: {state!r}")
        vistos.append(state)
        sembradas = {"existe una póliza proyectada", "hay pólizas proyectadas"}
        lectura.polizas = [_poliza()] if state in sembradas else []

    with _servir(app) as url:
        Verifier("consulta", host="127.0.0.1").add_source(PACTO).add_transport(
            url=url
        ).state_handler(estado, teardown=True).verify()
    assert set(vistos) == ESTADOS
