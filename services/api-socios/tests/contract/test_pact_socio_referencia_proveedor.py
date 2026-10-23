"""HU-W04 AC2: `api-socios` verifica como proveedor el pacto del socio de referencia.

La app real (autenticación, permiso COTIZAR, DTO v1 y la regla de la oferta vencida) se sirve con
uvicorn; `cotizacion` es un puerto simulado que el manejador de estados configura. El pacto lo
genera test_pact_socio_referencia.py (se ejecuta antes en el mismo `make test-contract`).
"""

import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import uvicorn
from api_socios.adapters.inbound.dependencias import consultar_oferta, cotizar_oferta
from api_socios.application.ofertas import ConsultarOferta, CotizarOferta
from api_socios.domain.ofertas import (
    CoberturaOferta,
    EstadoOferta,
    ExclusionResumida,
    Oferta,
    OfertaNoEncontrada,
)
from api_socios.domain.socios import CanalVenta, Permiso, Plan, Socio
from api_socios.main import app
from pact import Verifier

PACTO = Path(__file__).parents[4] / "contracts" / "pact" / "socio-referencia-api-socios.json"
VIGENTE = UUID("00000000-0000-4000-d000-000000000010")
VENCIDA = UUID("00000000-0000-4000-d000-000000000011")


def _oferta(cotizacion_id: UUID, estado: EstadoOferta) -> Oferta:
    return Oferta(
        id=UUID("00000000-0000-4000-e000-000000000010"),
        codigo="COT-2026-00010",
        cotizacion_id=cotizacion_id,
        producto_id="vida-hipotecario",
        precio=Decimal(97460),
        moneda="COP",
        periodicidad="MENSUAL",
        coberturas=(
            CoberturaOferta("MUERTE", "Muerte por cualquier causa", Decimal(180000000)),
            CoberturaOferta("ENFERMEDADES_GRAVES", "Enfermedades graves", Decimal(54000000)),
        ),
        exclusiones=(ExclusionResumida("SUICIDIO_PRIMER_ANIO", "Suicidio el primer año"),),
        version=1,
        version_reglas="2026.10.1",
        creada_en=datetime(2026, 10, 22, 15, tzinfo=UTC),
        vigente_hasta=datetime(2026, 10, 29, 15, tzinfo=UTC),
        estado=estado,
    )


class CotizacionSimulada:
    """Solo responde lo que el estado de la interacción sembró: lo demás no existe."""

    def __init__(self) -> None:
        self.ofertas: dict[UUID, Oferta] = {}
        self.socios: list[str] = []

    async def cotizar(self, solicitud: Any, socio_id: str, clave: UUID) -> tuple[Oferta, bool]:
        self.socios.append(socio_id)
        return _oferta(VIGENTE, EstadoOferta.VIGENTE), True

    async def oferta(self, cotizacion_id: UUID, socio_id: str) -> Oferta:
        self.socios.append(socio_id)
        if cotizacion_id not in self.ofertas:
            raise OfertaNoEncontrada()
        return self.ofertas[cotizacion_id]


@pytest.fixture
def proveedor(entorno: Any) -> Iterator[str]:
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


def test_api_socios_cumple_el_pacto_del_socio_de_referencia(entorno: Any, proveedor: str) -> None:
    assert PACTO.exists(), "Ejecutar primero test_pact_socio_referencia.py (genera el pacto)"
    simulada = CotizacionSimulada()
    app.dependency_overrides[cotizar_oferta] = lambda: CotizarOferta(simulada)
    app.dependency_overrides[consultar_oferta] = lambda: ConsultarOferta(simulada)
    vistos: list[str] = []

    def estado(state: str, action: str) -> None:
        if action != "setup":
            return
        vistos.append(state)
        if state == "el socio SOC-000001 con permiso cotizar existe":
            entorno.repositorio.socios.clear()
            simulada.ofertas.clear()
            entorno.repositorio.guardar(
                Socio(
                    id="SOC-000001",
                    nombre="Socio de referencia S.A.S.",
                    canal=CanalVenta.PLATAFORMA_DIGITAL,
                    plan=Plan.BASICO,
                    permisos=frozenset({Permiso.COTIZAR}),
                    client_id="cliente-socio-referencia",
                    api_key_id="key-socio-referencia",
                )
            )
        elif state == f"la cotización {VIGENTE} del socio tiene una oferta vigente":
            simulada.ofertas[VIGENTE] = _oferta(VIGENTE, EstadoOferta.VIGENTE)
        elif state == f"la oferta de la cotización {VENCIDA} del socio venció":
            simulada.ofertas[VENCIDA] = _oferta(VENCIDA, EstadoOferta.VENCIDA)
        elif not state.endswith("es de otro socio"):
            raise AssertionError(f"estado del proveedor sin sembrar: {state!r}")

    Verifier("api-socios", host="127.0.0.1").add_source(PACTO).add_transport(
        url=proveedor
    ).state_handler(estado, teardown=True).verify()
    # Cada operación se pidió como el socio autenticado, nunca como otro.
    assert set(simulada.socios) == {"SOC-000001"}
    assert len(vistos) == 7
